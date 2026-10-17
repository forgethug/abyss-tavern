"""深渊酒馆 S1 自动组队：地牢驱动 + auto 模式并入。

**新版流程**（与上赛季和旧 S1 实现都不同）：

① `seed_empty_parties_for_rank_distribution()`：按当前可派遣的玩家自建冒险者的
   rank 分布开「空壳小队」——每个 rank 同时最多 N 支 forming（默认 2，exploring 不计）。
   受日配额预测、慢时段概率节流与同 rank 冷却约束。空壳队 target_dungeon 在该档地牢
   列表里随机选，target_size 随机 `[min_size, max_size]`，不设 leader。

② `form_s1_parties()`：把 `party_preference == 'auto'` 且精力够的 idle 冒险者
   按跨度意愿并入①创建的空壳队。**只并入、不再建队**——新版下任何新队伍都由①
   负责，避免「玩家手动模式冒险者也被自动建出空队」、避免上赛季漂移门那套多轮
   累积的复杂度。

`manual` 模式冒险者不参与自动并入，他们通过 `/parties/<id>/join` 自己点加入；
但他们的等级仍计入①的 rank 分布（"现有冒险者实力"包含全部 idle 玩家自建冒险者，
manual 模式只决定加入方式，不代表他们不去探险）。

跨度匹配工具来自 S1 专属的 `adventurer_s1_config_service`。
"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import player_database_result
import random
from contextlib import nullcontext
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy import func
from db_repository import DatabaseError, db_manager
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Party, AdventurerS1PartyMember, AdventurerS1PartyRejoinCooldown, AdventurerS1PartyDepartureVote, AdventurerS1SpecialPartySlot
from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_ACTIVE_DUNGEONS, S1_DUNGEON_RANK_MAP, SPAN_MAX_HIGHER, check_s1_join_dungeon_rank_span, get_adventurer_real_rank_s1, get_s1_joinable_ranks, get_s1_special_dungeon, get_s1_special_dungeon_ticket, is_s1_special_dungeon
from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import AdventurerS1StateTransitionCoordinator
from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
JOIN_PROB_SAME_RANK = 0.9
JOIN_PROB_HIGHER_1 = 0.5
JOIN_PROB_LOWER_1 = 0.6
JOIN_PROB_LOWER_2 = 0.4

def _party_setting(key: str, default: int) -> int:
    """读取组队相关持久化设置（优先 settings 表）。"""
    if key == 'party_min_size':
        return 1
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    try:
        val = S1SettingsService.get(key)
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def _int_cfg(key: str, default: int) -> int:
    try:
        return int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        return default

def _live_stamina_for_adv(adv, *, stamina_max: int, recovery_rate: int, tavern_seated: bool) -> int:
    from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
    return get_live_stamina(adv, max_stamina=stamina_max, recovery_per_minute=recovery_rate, tavern_seated=tavern_seated)

def _rejoin_cooldown_minutes() -> int:
    return _int_cfg('ADVENTURER_S1_PARTY_REJOIN_COOLDOWN_MINUTES', 5)

def is_expedition_dispatch_paused() -> bool:
    """全服是否静默暂停发车（仅调度层拦截，不影响玩家组队/投票/倒计时）。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    return bool(S1SettingsService.get('expedition_dispatch_paused'))

def is_auto_dispatch_on_full_enabled() -> bool:
    """满员后是否自动倒计时并允许调度层发车（ADVENTURER_S1_AUTO_DISPATCH_ON_FULL）。"""
    return bool(current_app.config.get('ADVENTURER_S1_AUTO_DISPATCH_ON_FULL', True))

def _initial_departure_seconds(party: AdventurerS1Party) -> int:
    """首次满员时的完整等待秒数（含赞助减时）。"""
    departure_mins = _party_setting('departure_minutes', 30)
    reduced_mins = getattr(party, 'sponsor_reduced_minutes', 0) or 0
    return max(0, departure_mins * 60 - reduced_mins * 60)

def _start_or_resume_departure_countdown(party: AdventurerS1Party) -> None:
    """满员时启动或恢复出发倒计时；未满员时不调用。"""
    if not is_auto_dispatch_on_full_enabled():
        return
    if party.departure_time is not None:
        return
    now = _portable_utc_now()
    paused = party.departure_paused_seconds
    if paused is not None:
        seconds = max(0, int(paused))
        party.departure_paused_seconds = None
    else:
        seconds = _initial_departure_seconds(party)
    party.departure_time = now + timedelta(seconds=seconds)

def _pause_departure_countdown(party: AdventurerS1Party) -> None:
    """未满员时暂停倒计时，冻结剩余秒数（不满员不计时）。"""
    if party.departure_time is None:
        _clear_departure_votes(party.id)
        return
    now = _portable_utc_now()
    remaining = max(0, int((party.departure_time - now).total_seconds()))
    party.departure_paused_seconds = remaining
    party.departure_time = None
    _clear_departure_votes(party.id)

def _clear_departure_votes(party_id: int) -> None:
    """清空小队出发投票（离队、暂停倒计时、解散、发车时调用）。"""
    AdventurerS1PartyDepartureVote.query.filter_by(party_id=party_id).delete()

def _on_party_membership_changed(party_id: int) -> None:
    """成员变动时清空出发投票，所有人需重新投票。"""
    _clear_departure_votes(party_id)

def _count_member_departure_votes(party_id: int) -> int:
    """仅统计当前成员的有效投票数（忽略已离队者的残留票）。"""
    return AdventurerS1PartyDepartureVote.query.join(AdventurerS1PartyMember, (AdventurerS1PartyMember.party_id == AdventurerS1PartyDepartureVote.party_id) & (AdventurerS1PartyMember.adventurer_id == AdventurerS1PartyDepartureVote.adventurer_id)).filter(AdventurerS1PartyDepartureVote.party_id == party_id).count()

def get_departure_votes_by_party_ids(party_ids: list[int]) -> dict[int, list[int]]:
    """批量查询各小队的投票冒险者 id 列表（仅当前在队成员，列表 API 用）。"""
    if not party_ids:
        return {}
    rows = AdventurerS1PartyDepartureVote.query.join(AdventurerS1PartyMember, (AdventurerS1PartyMember.party_id == AdventurerS1PartyDepartureVote.party_id) & (AdventurerS1PartyMember.adventurer_id == AdventurerS1PartyDepartureVote.adventurer_id)).filter(AdventurerS1PartyDepartureVote.party_id.in_(party_ids)).with_entities(AdventurerS1PartyDepartureVote.party_id, AdventurerS1PartyDepartureVote.adventurer_id).all()
    bucket: dict[int, list[int]] = {}
    for party_id, adventurer_id in rows:
        bucket.setdefault(party_id, []).append(adventurer_id)
    return bucket

def _maybe_accelerate_departure_by_votes(party: AdventurerS1Party) -> bool:
    """若全体成员已投票，将 departure_time 设为当前时刻以便下一 tick 发车。"""
    member_count = AdventurerS1PartyMember.query.filter_by(party_id=party.id).count()
    if member_count < party.target_size:
        return False
    vote_count = _count_member_departure_votes(party.id)
    if vote_count < member_count:
        return False
    now = _portable_utc_now()
    if party.departure_time and party.departure_time <= now:
        return True
    party.departure_time = now
    party.departure_paused_seconds = None
    return True

def _check_rejoin_cooldown(adventurer_id: int, party_id: int) -> str | None:
    """若仍在同队重入冷却期内，返回玩家可见错误文案。"""
    cooldown_mins = _rejoin_cooldown_minutes()
    if cooldown_mins <= 0:
        return None
    now = _portable_utc_now()
    row = AdventurerS1PartyRejoinCooldown.query.filter_by(adventurer_id=adventurer_id, party_id=party_id).first()
    if not row or row.blocked_until <= now:
        return None
    wait_secs = int((row.blocked_until - now).total_seconds())
    wait_mins = max(1, (wait_secs + 59) // 60)
    return f'离队后 {wait_mins} 分钟内不能再次加入该小队'

def _record_party_leave_cooldown(adventurer_id: int, party_id: int) -> None:
    cooldown_mins = _rejoin_cooldown_minutes()
    if cooldown_mins <= 0:
        return
    blocked_until = _portable_utc_now() + timedelta(minutes=cooldown_mins)
    row = AdventurerS1PartyRejoinCooldown.query.filter_by(adventurer_id=adventurer_id, party_id=party_id).first()
    if row:
        row.blocked_until = blocked_until
        return
    db_manager.add(AdventurerS1PartyRejoinCooldown(adventurer_id=adventurer_id, party_id=party_id, blocked_until=blocked_until))

def _dungeons_by_rank(closed_dungeons: set[str] | None=None) -> dict:
    """rank → 该档地牢名列表。供新建空壳队时按档随机挑名字。"""
    closed = closed_dungeons or set()
    bucket: dict[int, list[str]] = {}
    for d in S1_ACTIVE_DUNGEONS:
        if d['name'] in closed:
            continue
        rank = S1_DUNGEON_RANK_MAP.get(d['name'], 0)
        bucket.setdefault(rank, []).append(d['name'])
    return bucket

def _forming_count_by_rank(closed_dungeons: set[str] | None=None) -> dict[int, int]:
    """各 rank 当前 forming 小队数量（exploring 不计入）。"""
    closed = closed_dungeons or set()
    all_forming = AdventurerS1Party.query.filter(AdventurerS1Party.status == 'forming').all()
    counts: dict[int, int] = {}
    for p in all_forming:
        if p.target_dungeon in closed:
            continue
        rank = S1_DUNGEON_RANK_MAP.get(p.target_dungeon, 0)
        counts[rank] = counts.get(rank, 0) + 1
    return counts

def _ranks_at_forming_cap(forming_counts: dict[int, int] | None=None) -> set[int]:
    """已达同 rank forming 数量上限的 rank 集合。"""
    max_forming = _party_setting('max_forming_per_rank', 2)
    counts = forming_counts if forming_counts is not None else _forming_count_by_rank()
    return {rank for rank, cnt in counts.items() if cnt >= max_forming}

def _ranks_in_cooldown(forming_counts: dict[int, int] | None=None, closed_dungeons: set[str] | None=None) -> set[int]:
    """同 rank 冷却：exploring 且出发未满 N 分钟时，若已有 forming 则不再 seed。

    disbanded 不计入；允许首支 forming 在 exploring 期间开出（并行组队）。
    """
    cooldown_mins = _party_setting('rank_cooldown_minutes', 15)
    if cooldown_mins <= 0:
        return set()
    if forming_counts is None:
        forming_counts = _forming_count_by_rank()
    cutoff = _portable_utc_now() - timedelta(minutes=cooldown_mins)
    exploring = AdventurerS1Party.query.filter(AdventurerS1Party.status == 'exploring', AdventurerS1Party.departure_time.isnot(None), AdventurerS1Party.departure_time > cutoff).all()
    blocked: set[int] = set()
    closed = closed_dungeons or set()
    for p in exploring:
        if p.target_dungeon in closed:
            continue
        rank = S1_DUNGEON_RANK_MAP.get(p.target_dungeon, 0)
        if forming_counts.get(rank, 0) >= 1:
            blocked.add(rank)
    return blocked

def _eligible_advs_for_party_pool():
    """筛出可计入「实力分布」的冒险者：玩家自建 + idle/resting + 精力 > 全局门槛。

    auto 与 manual 都计入，因为两种模式只决定加入队伍的方式（自动并入 vs 手动点
    加入），不代表 manual 模式的冒险者不去探险——他们的等级也是当前实力分布的
    一部分，建空壳队时按他们的 rank 也要开档。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    recovery_rate = _int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    raw = AdventurerS1.query.filter(AdventurerS1.status.in_(AdventurerS1Service.FREE_STATUSES), AdventurerS1.is_player_created == True).all()
    from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
    seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id for adv in raw])
    return [adv for adv in raw if _live_stamina_for_adv(adv, stamina_max=stamina_max, recovery_rate=recovery_rate, tavern_seated=adv.id in seated_ids) > stamina_min]

def seed_empty_parties_for_rank_distribution(min_size: int | None=None, max_size: int | None=None) -> list[AdventurerS1Party]:
    """按 idle 玩家自建冒险者的 rank 分布建空壳 forming 队。

    规则：
    1. 每个 rank 同时最多 N 支 forming（默认 2）；exploring 不计入。
    2. 该 rank 当前没有任何 idle 玩家自建冒险者时也不建（避免开出永远没人的空队）。
    3. 满足条件则建 1 支：target_dungeon 在该档地牢列表里随机选，target_size 随机
       `[min_size, max_size]`，leader_id 留 NULL，无成员。

    返回本轮新建的小队列表。
    """
    min_size = min_size or _party_setting('party_min_size', 4)
    max_size = max_size or _party_setting('party_max_size', 6)
    pool = _eligible_advs_for_party_pool()
    if not pool:
        return []
    rank_counts: dict[int, int] = {}
    for adv in pool:
        rank = get_adventurer_real_rank_s1(adv)
        rank_counts[rank] = rank_counts.get(rank, 0) + 1
    closed_dungeons = S1WorldLocationStateService.closed_dungeon_names((d['name'] for d in S1_ACTIVE_DUNGEONS))
    dungeons_by_rank = _dungeons_by_rank(closed_dungeons)
    forming_counts = _forming_count_by_rank(closed_dungeons)
    cap_ranks = _ranks_at_forming_cap(forming_counts)
    cooldowned_ranks = _ranks_in_cooldown(forming_counts, closed_dungeons)
    created: list[AdventurerS1Party] = []
    with db_manager.transaction():
        for rank in sorted(rank_counts.keys()):
            if rank_counts[rank] <= 0:
                continue
            if rank in cap_ranks:
                continue
            if rank in cooldowned_ranks:
                continue
            names = dungeons_by_rank.get(rank, [])
            if not names:
                continue
            dungeon_name = random.choice(names)
            target_size = random.randint(min_size, max_size)
            party = AdventurerS1Party(name=f'小队_{random.randint(100, 999)}', target_dungeon=dungeon_name, target_size=target_size, status='forming')
            db_manager.add(party)
            db_manager.flush()
            created.append(party)
    return created

def form_s1_parties() -> list[AdventurerS1Party]:
    """把 `auto` 模式 idle 冒险者按跨度意愿并入现有空壳队。

    **不再建队**：本轮没有合适的现有 forming 队就跳过，建队由
    `seed_empty_parties_for_rank_distribution()` 独立负责。

    并入规则：
    - 仅 `party_preference == 'auto'` 的 idle/resting 玩家自建冒险者参与
    - 必须精力 > `ADVENTURER_S1_STAMINA_MIN_TO_ENTER`
    - 副本相对冒险者：最多高 +1 档、低档不限；同档/低档按意愿概率决定是否加入
    - 满员则启动/恢复 departure_time（暂停剩余秒数续计，首次满员才用完整等待时长）

    返回值仅为了向后兼容签名（旧版返回新建队列表）；现新建队不在这里发生，固定返回 []。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    recovery_rate = _int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    candidates = [adv for adv in AdventurerS1.query.filter(AdventurerS1.status.in_(AdventurerS1Service.FREE_STATUSES), AdventurerS1.is_player_created == True, AdventurerS1.party_preference == 'auto').all()]
    if not candidates:
        return []
    random.shuffle(candidates)
    forming_parties_objs = AdventurerS1Party.query.filter_by(status='forming').all()
    if not forming_parties_objs:
        return []
    closed_dungeons = S1WorldLocationStateService.closed_dungeon_names((p.target_dungeon for p in forming_parties_objs))
    forming_parties = [{'id': p.id, 'target_size': p.target_size, 'target_dungeon': p.target_dungeon, 'member_count': len(p.members)} for p in forming_parties_objs if not is_s1_special_dungeon(p.target_dungeon) and p.target_dungeon not in closed_dungeons]
    if not forming_parties:
        return []
    for adv in candidates:
        if all((p['member_count'] >= p['target_size'] for p in forming_parties)):
            break
        with db_manager.transaction():
            locked_adv = AdventurerS1.query.filter_by(id=adv.id).with_for_update().first()
            if not locked_adv:
                continue
            can, _reason = locked_adv.can_dispatch()
            if not can:
                continue
            adv = locked_adv
            adv_rank = get_adventurer_real_rank_s1(adv)
            joinable_ranks = get_s1_joinable_ranks(adv)
            available = [p for p in forming_parties if p['member_count'] < p['target_size']]
            if not available:
                continue
            willing: list[dict] = []
            for p in available:
                target_rank = S1_DUNGEON_RANK_MAP.get(p['target_dungeon'], 0)
                if target_rank not in joinable_ranks:
                    continue
                if target_rank > adv_rank:
                    rank_diff, is_higher = (target_rank - adv_rank, True)
                elif target_rank < adv_rank:
                    rank_diff, is_higher = (adv_rank - target_rank, False)
                else:
                    rank_diff, is_higher = (0, None)
                if is_higher is True and rank_diff > SPAN_MAX_HIGHER:
                    continue
                if rank_diff == 0:
                    prob = JOIN_PROB_SAME_RANK
                elif is_higher:
                    prob = JOIN_PROB_HIGHER_1
                elif rank_diff == 1:
                    prob = JOIN_PROB_LOWER_1
                else:
                    prob = JOIN_PROB_LOWER_2
                if random.random() > prob:
                    continue
                willing.append(p)
            if not willing:
                continue
            chosen = random.choice(willing)
            rejoin_err = _check_rejoin_cooldown(adv.id, chosen['id'])
            if rejoin_err:
                continue
            locked_party = AdventurerS1Party.query.filter_by(id=chosen['id']).with_for_update().first()
            if not locked_party or locked_party.status != 'forming':
                continue
            real_count = AdventurerS1PartyMember.query.filter_by(party_id=chosen['id']).count()
            chosen['member_count'] = real_count
            if real_count >= locked_party.target_size:
                continue
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
            tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
            current_stamina = _live_stamina_for_adv(adv, stamina_max=stamina_max, recovery_rate=recovery_rate, tavern_seated=tavern_seated)
            if current_stamina <= stamina_min:
                continue
            AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'in_party')
            if adv.stamina <= stamina_min:
                AdventurerS1StateTransitionCoordinator.enter_rest(adv)
                continue
            db_manager.flush()
            db_manager.add(AdventurerS1PartyMember(party_id=chosen['id'], adventurer_id=adv.id))
            chosen['member_count'] = real_count + 1
            _on_party_membership_changed(chosen['id'])
            if real_count + 1 >= locked_party.target_size:
                _start_or_resume_departure_countdown(locked_party)
    return []

def tick_party_formation() -> dict:
    """组队主循环 tick：自动偏好玩家报名后，新 S1 匹配队列按发车配额成团。

    供调度器和 admin 手动接口共用。返回简短统计便于日志/接口回显。
    """
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import AdventurerS1MatchmakingService
    return AdventurerS1MatchmakingService.tick_matchmaking()

def disband_all_legacy_normal_parties() -> dict:
    """解散未出发的旧版普通小队（非匹配成团、非特殊机缘）。幂等，无剩余目标时为空操作。"""
    from plugins.adventurer_s1.models import AdventurerS1MatchTicket
    from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
    matched_ticket_exists = AdventurerS1MatchTicket.query.filter(AdventurerS1MatchTicket.matched_party_id == AdventurerS1Party.id).exists()
    q = AdventurerS1Party.query.filter(AdventurerS1Party.status == 'forming', ~matched_ticket_exists)
    disbanded_ids: list[int] = []
    for party in q.all():
        if is_s1_special_dungeon(party.target_dungeon):
            continue
        ok, _err = force_disband_party(party.id, require_unmatched_normal=True)
        if ok:
            disbanded_ids.append(party.id)
    if disbanded_ids:
        invalidate_parties_board_cache()
    return {'disbanded_count': len(disbanded_ids), 'party_ids': disbanded_ids}

def get_departed_parties() -> list[AdventurerS1Party]:
    """已到出发时间、仍 forming 且满员的小队（供调度/手动触发发起探险）。"""
    now = _portable_utc_now()
    member_count = db_manager.query(func.count(AdventurerS1PartyMember.id)).filter(AdventurerS1PartyMember.party_id == AdventurerS1Party.id).correlate(AdventurerS1Party).scalar_subquery()
    return AdventurerS1Party.query.filter(AdventurerS1Party.status == 'forming', AdventurerS1Party.departure_time.isnot(None), AdventurerS1Party.departure_time <= now, member_count >= AdventurerS1Party.target_size).order_by(AdventurerS1Party.departure_time.asc(), AdventurerS1Party.created_at.asc(), AdventurerS1Party.id.asc()).all()

def _check_span_for_join(adv, party) -> str | None:
    """跨度硬限制检查：向下不限（可进任意低档副本），向上最多高 1 rank。"""
    return check_s1_join_dungeon_rank_span(adv, party.target_dungeon)

def _adv_holds_special_dungeon_ticket(adventurer_id: int, dungeon_name: str | None) -> bool:
    """冒险者随身是否携带该特殊副本对应的门票（仓库不算）。"""
    ticket = get_s1_special_dungeon_ticket(dungeon_name)
    if not ticket:
        return False
    from plugins.adventurer_s1.services.s1_item_grant_service import adventurer_carries_item_name
    return adventurer_carries_item_name(adventurer_id, ticket)

class _SpecialPartySlotConflict(RuntimeError):
    """并发创建命中特殊副本唯一占位。"""

def _existing_forming_special_party(dungeon_name: str, *, lock: bool=False) -> AdventurerS1Party | None:
    """兼容旧数据地查找同副本现存 forming 队；可选行锁用于给出稳定提示。"""
    query = AdventurerS1Party.query.filter_by(status='forming', target_dungeon=dungeon_name).order_by(AdventurerS1Party.created_at.asc(), AdventurerS1Party.id.asc())
    if lock:
        query = query.with_for_update()
    return query.first()

def create_special_party(adv: AdventurerS1, dungeon_name: str) -> tuple[AdventurerS1Party | None, str | None]:
    """玩家用持票冒险者手动组建一支前往门票制特殊副本的小队，并把该冒险者作为队长加入。

    特殊副本固定 4 人（S1_SPECIAL_PARTY_TARGET_SIZE），创建后 status=forming（集结中）；
    满 4 人后才启动出发倒计时（见 join_party_manual）。校验：副本须为特殊副本、
    冒险者须持有对应门票、可派遣、精力达标。门票在「发车」时才扣除。返回 (party, err)。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_special_party_target_size
    special = get_s1_special_dungeon(dungeon_name)
    if not special:
        return (None, '该副本不是门票制特殊副本')
    if not special.get('entry_enabled', True):
        return (None, '该副本尚未开放')
    if not adv.is_player_created:
        return (None, '该冒险者不是玩家自建，不能组建小队')
    can, reason = adv.can_dispatch()
    if not can:
        return (None, reason)
    size = get_s1_special_party_target_size()
    stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    recovery_rate = _int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    try:
        with db_manager.transaction():
            locked_adv = AdventurerS1.query.filter_by(id=adv.id).with_for_update().first()
            if not locked_adv:
                return (None, '冒险者不存在')
            if locked_adv.status not in AdventurerS1Service.FREE_STATUSES:
                return (None, '冒险者当前忙碌，无法组建小队')
            adv = locked_adv
            existing_party = _existing_forming_special_party(dungeon_name, lock=True)
            if existing_party:
                return (None, f'【{dungeon_name}】已有集结中的小队，请直接加入现有小队')
            if not _adv_holds_special_dungeon_ticket(adv.id, dungeon_name):
                return (None, f"组建【{dungeon_name}】小队需要随身携带「{special['ticket_item']}」（仓库中的门票无效）")
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
            tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
            current_stamina = _live_stamina_for_adv(adv, stamina_max=stamina_max, recovery_rate=recovery_rate, tavern_seated=tavern_seated)
            if current_stamina <= stamina_min:
                return (None, f'精力不足（当前 {current_stamina}，需 > {stamina_min}）')
            party = AdventurerS1Party(name=f'{dungeon_name}·试炼小队', status='forming', target_dungeon=dungeon_name, target_size=size, leader_id=adv.id)
            ok, err = player_database_result(db_manager.add(party))
            if not ok:
                return (None, err)
            flushed, flush_err = player_database_result(db_manager.flush())
            if not flushed:
                raise RuntimeError(flush_err or '特殊副本小队创建失败')
            ok, err = player_database_result(db_manager.add(AdventurerS1SpecialPartySlot(dungeon_name=dungeon_name, party_id=party.id)))
            if not ok:
                raise RuntimeError(err or '特殊副本小队占位失败')
            slot_flushed, slot_flush_err = player_database_result(db_manager.flush())
            if not slot_flushed:
                raise _SpecialPartySlotConflict(slot_flush_err or '特殊副本小队占位冲突')
            AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'in_party')
            if adv.stamina <= stamina_min:
                AdventurerS1StateTransitionCoordinator.enter_rest(adv)
                return (None, f'精力不足（当前 {adv.stamina}，需 > {stamina_min}）')
            db_manager.flush()
            db_manager.add(AdventurerS1PartyMember(party_id=party.id, adventurer_id=adv.id))
            _on_party_membership_changed(party.id)
            new_party_id = party.id
    except DatabaseError as exc:
        if isinstance(exc.original_error, _SpecialPartySlotConflict):
            return (None, f'【{dungeon_name}】已有集结中的小队，请直接加入现有小队')
        raise
    return (AdventurerS1Party.query.session.get(AdventurerS1Party, new_party_id), None)

def join_party_manual(adv: AdventurerS1, party: AdventurerS1Party) -> tuple[bool, str | None]:
    """玩家手动让自己的冒险者加入指定 forming 队。须在事务外调用，内部自带事务。

    校验：
    - 队伍必须是 forming 状态且未满员
    - 冒险者必须是 idle/resting 玩家自建、当前不在队
    - 精力 > `ADVENTURER_S1_STAMINA_MIN_TO_ENTER`
    - rank 跨度硬限制（与 auto 并入同标准；意愿概率不再适用）

    返回 (ok, err)。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    if not adv.is_player_created:
        return (False, '该冒险者不是玩家自建，不能手动加入')
    can, reason = adv.can_dispatch()
    if not can:
        return (False, reason)
    if party.status != 'forming':
        return (False, '小队已出发或解散，无法加入')
    if not is_s1_special_dungeon(party.target_dungeon):
        return (False, '普通副本请使用匹配组队')
    if not get_s1_special_dungeon(party.target_dungeon).get('entry_enabled', True):
        return (False, '该副本尚未开放')
    if not _adv_holds_special_dungeon_ticket(adv.id, party.target_dungeon):
        ticket = get_s1_special_dungeon_ticket(party.target_dungeon)
        return (False, f'加入【{party.target_dungeon}】需要随身携带「{ticket}」（仓库中的门票无效）')
    rejoin_err = _check_rejoin_cooldown(adv.id, party.id)
    if rejoin_err:
        return (False, rejoin_err)
    stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    recovery_rate = _int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    with db_manager.transaction():
        locked_adv = AdventurerS1.query.populate_existing().filter_by(id=adv.id).with_for_update().first()
        if not locked_adv:
            return (False, '冒险者不存在')
        if locked_adv.status not in AdventurerS1Service.FREE_STATUSES:
            return (False, '冒险者当前忙碌，无法加入小队')
        adv = locked_adv
        locked_party = AdventurerS1Party.query.populate_existing().filter_by(id=party.id).with_for_update().first()
        if not locked_party or locked_party.status != 'forming':
            return (False, '小队已出发或解散，无法加入')
        party = locked_party
        current_count = AdventurerS1PartyMember.query.filter_by(party_id=party.id).count()
        if current_count >= party.target_size:
            return (False, '小队已满员')
        already_in = AdventurerS1PartyMember.query.filter_by(party_id=party.id, adventurer_id=adv.id).first()
        if already_in:
            return (False, '该冒险者已在此小队中')
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
        current_stamina = _live_stamina_for_adv(adv, stamina_max=stamina_max, recovery_rate=recovery_rate, tavern_seated=tavern_seated)
        if current_stamina <= stamina_min:
            return (False, f'精力不足（当前 {current_stamina}，需 > {stamina_min}）')
        AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'in_party')
        if adv.stamina <= stamina_min:
            AdventurerS1StateTransitionCoordinator.enter_rest(adv)
            return (False, f'精力不足（当前 {adv.stamina}，需 > {stamina_min}）')
        db_manager.flush()
        db_manager.add(AdventurerS1PartyMember(party_id=party.id, adventurer_id=adv.id))
        new_count = current_count + 1
        _on_party_membership_changed(party.id)
        if new_count >= party.target_size:
            _start_or_resume_departure_countdown(party)
    return (True, None)

def _unwind_matched_party_for_member_leave(party: AdventurerS1Party, *, locked_advs: list[AdventurerS1], current_member_rows: list[AdventurerS1PartyMember], leaver: AdventurerS1) -> tuple[dict | None, str | None]:
    """撤销尚未发车的普通匹配成团，并按原 ticket 单元恢复排队。

    调用方须先按 ``adventurer -> party`` 锁序锁住当前全体成员和 Party，且仍在
    同一事务内。没有关联 matched ticket 时返回 ``(None, None)``，由普通离队
    路径继续处理。
    """
    from plugins.adventurer_s1.models import AdventurerS1MatchTicket, AdventurerS1MatchTicketMember
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import _record_match_ticket_leave_cooldown
    matched_tickets = db_manager.query(AdventurerS1MatchTicket).populate_existing().filter_by(matched_party_id=party.id, status='matched').order_by(AdventurerS1MatchTicket.id.asc()).with_for_update().all()
    if not matched_tickets:
        return (None, None)
    ticket_ids = [ticket.id for ticket in matched_tickets]
    ticket_member_rows = db_manager.query(AdventurerS1MatchTicketMember).filter(AdventurerS1MatchTicketMember.ticket_id.in_(ticket_ids)).order_by(AdventurerS1MatchTicketMember.ticket_id.asc(), AdventurerS1MatchTicketMember.id.asc()).with_for_update().all()
    party_member_ids = {row.adventurer_id for row in current_member_rows}
    ticket_member_ids = {row.adventurer_id for row in ticket_member_rows}
    if len(ticket_member_rows) != len(ticket_member_ids):
        return (None, '匹配成团存在重复成员，暂不能退出，请联系管理员处理')
    if party_member_ids != ticket_member_ids:
        return (None, '匹配成团成员状态异常，暂不能退出，请刷新后重试或联系管理员')
    locked_adv_by_id = {item.id: item for item in locked_advs}
    if party_member_ids != set(locked_adv_by_id):
        return (None, '小队成员刚刚发生变化，请刷新后重试')
    member_rows_by_ticket: dict[int, list[AdventurerS1MatchTicketMember]] = {ticket_id: [] for ticket_id in ticket_ids}
    for row in ticket_member_rows:
        member_rows_by_ticket[row.ticket_id].append(row)
    if not any((row.adventurer_id == leaver.id for row in ticket_member_rows)):
        return (None, '未找到退出者对应的匹配报名，暂不能退出，请联系管理员')
    now = _portable_utc_now()
    from .s1_campaign_task_admission_service import S1CampaignTaskAdmissionService
    if S1CampaignTaskAdmissionService.cancel_locked_party(party, now=now):
        return ({'party_id': party.id, 'member_count': 0, 'matchmaking_changed': True, 'ticket_user_ids': sorted({t.owner_id for t in matched_tickets if t.owner_id} | {a.owner_id for a in locked_advs if a.owner_id}), 'cancelled_ticket_ids': ticket_ids, 'forming_ticket_ids': [], 'requeued_ticket_ids': []}, None)
    rest_adventurer_ids = {leaver.id}
    affected_user_ids = {int(item.owner_id) for item in locked_advs if item.owner_id is not None}
    cancelled_ticket_ids: list[int] = []
    forming_ticket_ids: list[int] = []
    requeued_ticket_ids: list[int] = []
    for ticket in matched_tickets:
        rows = member_rows_by_ticket[ticket.id]
        member_ids = {row.adventurer_id for row in rows}
        if ticket.owner_id is not None:
            affected_user_ids.add(int(ticket.owner_id))
        contains_leaver = leaver.id in member_ids
        if contains_leaver:
            _record_match_ticket_leave_cooldown(leaver.id, ticket)
            leaver_is_ticket_owner = leaver.owner_id is not None and ticket.owner_id == leaver.owner_id
            if ticket.queue_type == 'solo' or leaver_is_ticket_owner:
                ticket.status = 'cancelled'
                ticket.queued_at = None
                ticket.cancelled_at = now
                ticket.match_error = ''
                rest_adventurer_ids.update(member_ids)
                cancelled_ticket_ids.append(ticket.id)
            else:
                for row in rows:
                    if row.adventurer_id == leaver.id:
                        db_manager.delete_instance(row)
                remaining_member_ids = member_ids - {leaver.id}
                if remaining_member_ids:
                    ticket.status = 'forming'
                    ticket.queued_at = None
                    ticket.cancelled_at = None
                    ticket.match_error = f'{leaver.name} 已退出已成团小队，匹配已取消，请队长确认后重新开始匹配'
                    forming_ticket_ids.append(ticket.id)
                else:
                    ticket.status = 'cancelled'
                    ticket.queued_at = None
                    ticket.cancelled_at = now
                    ticket.match_error = ''
                    cancelled_ticket_ids.append(ticket.id)
        else:
            ticket.status = 'queued'
            ticket.queued_at = ticket.queued_at or now
            ticket.cancelled_at = None
            ticket.match_error = '已成团队员退出，已自动返回匹配队列'
            requeued_ticket_ids.append(ticket.id)
        ticket.matched_party_id = None
        ticket.matched_at = None
    for locked_adv in locked_advs:
        if locked_adv.id in rest_adventurer_ids and locked_adv.status == 'in_party':
            AdventurerS1StateTransitionCoordinator.enter_rest(locked_adv)
    db_manager.query(AdventurerS1PartyMember).filter_by(party_id=party.id).delete(synchronize_session=False)
    _clear_departure_votes(party.id)
    db_manager.query(AdventurerS1SpecialPartySlot).filter_by(party_id=party.id).delete(synchronize_session=False)
    party.status = 'disbanded'
    party.departure_time = None
    party.departure_paused_seconds = None
    return ({'party_id': party.id, 'member_count': 0, 'matchmaking_changed': True, 'ticket_user_ids': sorted(affected_user_ids), 'cancelled_ticket_ids': cancelled_ticket_ids, 'forming_ticket_ids': forming_ticket_ids, 'requeued_ticket_ids': requeued_ticket_ids}, None)

def leave_party(adv: AdventurerS1, *, auto_commit: bool=True) -> tuple[bool, str | None, dict | None]:
    """让冒险者离开当前所在的 forming 队。

    默认内部提交；已经持有完整外层事务的调用方必须传 ``auto_commit=False``，避免
    中途提交并提前释放成员/Party 行锁。

    限制：
    - 仅限当前在 forming 阶段的队伍可离；exploring/disbanded 一律拒绝
    - 离队后人数若 < target_size，暂停 departure_time 并冻结剩余秒数（不满员不计时）
    - 记录同队重入冷却，防止频繁离队刷倒计时
    - 冒险者状态回 resting；精力按惰性恢复继续累计

    返回 (ok, err, info)。info 含 party_id 和离队后的 member_count 供前端刷新。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    member_row = AdventurerS1PartyMember.query.join(AdventurerS1Party, AdventurerS1Party.id == AdventurerS1PartyMember.party_id).filter(AdventurerS1PartyMember.adventurer_id == adv.id, AdventurerS1Party.status == 'forming').first()
    if not member_row:
        return (False, '没有可离开的 forming 小队（可能已出发或解散）', None)
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, member_row.party_id)
    if not party:
        return (False, '小队不存在', None)
    if party.status != 'forming':
        return (False, '小队已出发，无法离队', None)
    initial_member_ids = sorted({row.adventurer_id for row in AdventurerS1PartyMember.query.filter_by(party_id=party.id).all()} | {adv.id})
    result_info: dict | None = None
    transaction_scope = db_manager.transaction() if auto_commit else nullcontext()
    with transaction_scope:
        locked_advs = AdventurerS1.query.populate_existing().filter(AdventurerS1.id.in_(initial_member_ids)).order_by(AdventurerS1.id.asc()).with_for_update().all()
        locked_adv_by_id = {item.id: item for item in locked_advs}
        locked_adv = locked_adv_by_id.get(adv.id)
        if not locked_adv:
            return (False, '冒险者不存在', None)
        locked_party = AdventurerS1Party.query.populate_existing().filter_by(id=party.id).with_for_update().first()
        if not locked_party:
            return (False, '小队不存在', None)
        if locked_party.status != 'forming':
            return (False, '小队已出发，无法离队', None)
        current_member_rows = AdventurerS1PartyMember.query.filter_by(party_id=locked_party.id).order_by(AdventurerS1PartyMember.id.asc()).all()
        current_member_ids = sorted((row.adventurer_id for row in current_member_rows))
        if current_member_ids != initial_member_ids:
            return (False, '小队成员刚刚发生变化，请刷新后重试', None)
        still_member = next((row for row in current_member_rows if row.adventurer_id == locked_adv.id), None)
        if not still_member:
            return (False, '冒险者已不在该小队中', None)
        matched_info, matched_err = _unwind_matched_party_for_member_leave(locked_party, locked_advs=locked_advs, current_member_rows=current_member_rows, leaver=locked_adv)
        if matched_err:
            return (False, matched_err, None)
        if matched_info is not None:
            result_info = matched_info
        else:
            db_manager.delete_instance(still_member)
            db_manager.flush()
            remaining = AdventurerS1PartyMember.query.filter_by(party_id=locked_party.id).count()
            _on_party_membership_changed(locked_party.id)
            if remaining < locked_party.target_size:
                _pause_departure_countdown(locked_party)
            _record_party_leave_cooldown(locked_adv.id, locked_party.id)
            if locked_adv.status == 'in_party':
                AdventurerS1StateTransitionCoordinator.enter_rest(locked_adv)
            result_info = {'party_id': locked_party.id, 'member_count': remaining}
    return (True, None, result_info)

def vote_party_departure(adv: AdventurerS1, party: AdventurerS1Party) -> tuple[bool, str | None, dict | None]:
    """成员投票提前出发。全体成员投赞成票后 departure_time 设为当前时刻，下一调度 tick 发车。

    返回 (ok, err, info)。info 含 vote_count、member_count、all_voted、departure_time。
    """
    if party.status != 'forming':
        return (False, '小队已出发或解散，无法投票', None)
    member_row = AdventurerS1PartyMember.query.filter_by(party_id=party.id, adventurer_id=adv.id).first()
    if not member_row:
        return (False, '该冒险者不在此小队中', None)
    if adv.status != 'in_party':
        return (False, '冒险者当前正在打工/建造，无法投票出发；如需退出请先点离队', None)
    member_count = AdventurerS1PartyMember.query.filter_by(party_id=party.id).count()
    if member_count < party.target_size:
        return (False, f'小队尚未满员（{member_count}/{party.target_size}），满员后才能投票出发', None)
    now = _portable_utc_now()
    if party.departure_time and party.departure_time <= now:
        return (False, '小队已准备好出发，无需再投票', None)
    with db_manager.transaction():
        party_locked = AdventurerS1Party.query.filter_by(id=party.id).with_for_update().first()
        if not party_locked or party_locked.status != 'forming':
            return (False, '小队状态已变更，无法投票', None)
        member_count = AdventurerS1PartyMember.query.filter_by(party_id=party.id).count()
        if member_count < party_locked.target_size:
            return (False, f'小队尚未满员（{member_count}/{party_locked.target_size}），满员后才能投票出发', None)
        if party_locked.departure_time and party_locked.departure_time <= now:
            return (False, '小队已准备好出发，无需再投票', None)
        existing = AdventurerS1PartyDepartureVote.query.filter_by(party_id=party.id, adventurer_id=adv.id).first()
        if not existing:
            db_manager.add(AdventurerS1PartyDepartureVote(party_id=party.id, adventurer_id=adv.id))
            db_manager.flush()
        vote_count = _count_member_departure_votes(party.id)
        all_voted = _maybe_accelerate_departure_by_votes(party_locked)
    departure_time = party_locked.departure_time.isoformat() if party_locked.departure_time else None
    return (True, None, {'party_id': party.id, 'adventurer_id': adv.id, 'vote_count': vote_count, 'member_count': member_count, 'all_voted': all_voted, 'departure_time': departure_time})

def force_disband_party(party_id: int, *, require_unmatched_normal: bool=False) -> tuple[bool, str | None]:
    """管理员强制解散小队。forming / exploring 均可。

    - forming：队员全部回 resting，删 PartyMember 行，party → disbanded
    - exploring：同上，还要把关联的进行中探险日志标为 failed

    返回 (ok, err)。
    """
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog, AdventurerS1MatchTicket
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    with db_manager.transaction():
        initial_member_ids = [m.adventurer_id for m in AdventurerS1PartyMember.query.filter_by(party_id=party_id).all()]
        if initial_member_ids:
            AdventurerS1.query.populate_existing().filter(AdventurerS1.id.in_(initial_member_ids)).order_by(AdventurerS1.id).with_for_update().all()
        party = AdventurerS1Party.query.populate_existing().filter_by(id=party_id).with_for_update().first()
        if not party:
            return (False, '小队不存在')
        if party.status == 'disbanded':
            return (False, '小队已经解散')
        if require_unmatched_normal:
            if is_s1_special_dungeon(party.target_dungeon):
                return (False, '特殊机缘小队不属于旧版普通小队')
            matched_ticket = AdventurerS1MatchTicket.query.filter_by(matched_party_id=party.id).with_entities(AdventurerS1MatchTicket.id).first()
            if matched_ticket:
                return (False, '匹配成团小队不属于旧版普通小队')
        if party.status == 'exploring':
            from plugins.adventurer_s1.services.s1_narrative_lease_service import release_narrative_leases_for_log
            logs = AdventurerS1ExpeditionLog.query.filter(AdventurerS1ExpeditionLog.party_id == party.id, AdventurerS1ExpeditionLog.status.in_(['generating', 'in_progress'])).all()
            for log in logs:
                log.status = 'failed'
                log.result_summary = '管理员强制解散'
                log.full_log = {**(log.full_log or {}), 'status': 'failed', 'summary': '管理员强制解散'}
                release_narrative_leases_for_log(log.id)
        member_ids = [m.adventurer_id for m in AdventurerS1PartyMember.query.filter_by(party_id=party.id).all()]
        finalize_party_disband(party.id, member_ids=member_ids)
    return (True, None)

def admin_remove_member(party_id: int, adventurer_id: int) -> tuple[bool, str | None]:
    """管理员从小队中移除指定成员。forming / exploring 均可。

    - 移除后冒险者回 resting
    - forming 队：若剩余人数 < target_size 则暂停出发倒计时
    - exploring 队：不影响已发起的探险（探险进度已锁定）
    - 不记录重入冷却（管理员操作）
    - 清空出发投票（成员变动）

    返回 (ok, err)。
    """
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
    if not party:
        return (False, '小队不存在')
    if party.status not in ('forming', 'exploring'):
        return (False, f'小队当前状态为 {party.status}，无法移除成员')
    member_row = AdventurerS1PartyMember.query.filter_by(party_id=party.id, adventurer_id=adventurer_id).first()
    if not member_row:
        return (False, '该冒险者不在此小队中')
    with db_manager.transaction():
        locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
        if not locked_adv:
            return (False, '冒险者不存在')
        db_manager.delete_instance(member_row)
        db_manager.flush()
        if locked_adv.status == 'in_party':
            AdventurerS1Service.enter_rest(locked_adv)
        if party.status == 'forming':
            remaining = AdventurerS1PartyMember.query.filter_by(party_id=party.id).count()
            _on_party_membership_changed(party.id)
            if remaining < party.target_size:
                _pause_departure_countdown(party)
        else:
            _on_party_membership_changed(party.id)
    return (True, None)

def _adventurer_ids_with_live_party_membership(adventurer_ids: list[int]) -> set[int]:
    """当前在 forming / exploring 小队中的冒险者 id 集合。"""
    if not adventurer_ids:
        return set()
    rows = db_manager.query(AdventurerS1PartyMember.adventurer_id).join(AdventurerS1Party, AdventurerS1Party.id == AdventurerS1PartyMember.party_id).filter(AdventurerS1PartyMember.adventurer_id.in_(adventurer_ids), AdventurerS1Party.status.in_(['forming', 'exploring'])).distinct().all()
    return {adv_id for adv_id, in rows}

def _adventurer_ids_with_active_match_ticket(adventurer_ids: list[int]) -> set[int]:
    """当前在 forming / queued 匹配报名中的冒险者 id 集合。"""
    if not adventurer_ids:
        return set()
    from plugins.adventurer_s1.models import AdventurerS1MatchTicket, AdventurerS1MatchTicketMember
    rows = db_manager.query(AdventurerS1MatchTicketMember.adventurer_id).join(AdventurerS1MatchTicket, AdventurerS1MatchTicket.id == AdventurerS1MatchTicketMember.ticket_id).filter(AdventurerS1MatchTicketMember.adventurer_id.in_(adventurer_ids), AdventurerS1MatchTicket.status.in_(['forming', 'queued'])).distinct().all()
    return {adv_id for adv_id, in rows}

def find_ghost_in_party_adventurers(*, limit: int=500) -> list[dict]:
    """列出「幽灵组队」冒险者：status=in_party，但不在任何活跃小队或匹配队列中。"""
    in_party_rows = db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id).filter_by(status='in_party').order_by(AdventurerS1.updated_at.desc()).limit(limit).all()
    if not in_party_rows:
        return []
    adv_ids = [row.id for row in in_party_rows]
    live_party_ids = _adventurer_ids_with_live_party_membership(adv_ids)
    active_ticket_ids = _adventurer_ids_with_active_match_ticket(adv_ids)
    stale_counts = dict(db_manager.query(AdventurerS1PartyMember.adventurer_id, func.count(AdventurerS1PartyMember.id)).filter(AdventurerS1PartyMember.adventurer_id.in_(adv_ids)).group_by(AdventurerS1PartyMember.adventurer_id).all())
    ghosts: list[dict] = []
    for row in in_party_rows:
        if row.id in live_party_ids or row.id in active_ticket_ids:
            continue
        ghosts.append({'adventurer_id': row.id, 'name': row.name, 'owner_id': row.owner_id, 'stale_member_rows': int(stale_counts.get(row.id, 0))})
    return ghosts

def admin_preview_ghost_in_party() -> tuple[dict, str | None]:
    """预览将被修复的幽灵组队冒险者（只读）。"""
    ghosts = find_ghost_in_party_adventurers()
    return ({'ghosts': ghosts, 'count': len(ghosts)}, None)

def _delete_stale_disbanded_party_members(adventurer_id: int) -> int:
    """删除该冒险者在已解散小队上的历史 PartyMember 行。"""
    disbanded_party_ids = db_manager.query(AdventurerS1Party.id).filter(AdventurerS1Party.status == 'disbanded').subquery()
    return db_manager.query(AdventurerS1PartyMember).filter(AdventurerS1PartyMember.adventurer_id == adventurer_id, AdventurerS1PartyMember.party_id.in_(disbanded_party_ids)).delete(synchronize_session=False)

def admin_repair_ghost_in_party() -> tuple[dict, str | None]:
    """修复幽灵组队：enter_rest + 清理 disbanded 队伍上的 stale member 行（幂等）。"""
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import _after_matchmaking_mutation
    preview = find_ghost_in_party_adventurers()
    if not preview:
        return ({'repaired': [], 'count': 0, 'stale_members_deleted': 0}, None)
    repaired: list[dict] = []
    stale_members_deleted = 0
    with db_manager.transaction():
        for item in preview:
            adv_id = item['adventurer_id']
            locked = db_manager.query(AdventurerS1).populate_existing().filter_by(id=adv_id).with_for_update().first()
            if not locked or locked.status != 'in_party':
                continue
            if _adventurer_ids_with_live_party_membership([adv_id]):
                continue
            if _adventurer_ids_with_active_match_ticket([adv_id]):
                continue
            AdventurerS1Service.enter_rest(locked)
            deleted = _delete_stale_disbanded_party_members(adv_id)
            stale_members_deleted += deleted
            repaired.append({'adventurer_id': locked.id, 'name': locked.name, 'stale_members_deleted': deleted})
    if repaired:
        invalidate_parties_board_cache()
        _after_matchmaking_mutation()
    return ({'repaired': repaired, 'count': len(repaired), 'stale_members_deleted': stale_members_deleted}, None)

def finalize_party_disband(party_id: int, *, member_ids: list[int] | None=None, exploring_stamina_refund: int | None=None, match_error: str | None=None, campaign_task_cooldown_until: datetime | None=None, cancelled_at: datetime | None=None) -> None:
    """小队解散后的统一收尾：成员回休息、删 PartyMember、清投票、收尾 matched 匹配单。

    须在已有 db_manager.transaction() 内调用；幂等。
    """
    from plugins.adventurer_s1.models import AdventurerS1MatchTicket
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    from sqlalchemy.orm import noload
    party = db_manager.query(AdventurerS1Party).options(*([noload('*')] if match_error is not None else [])).populate_existing().filter_by(id=party_id).first()
    if not party:
        return
    current_member_ids = [m.adventurer_id for m in db_manager.query(AdventurerS1PartyMember.adventurer_id).filter_by(party_id=party_id).all()]
    member_ids = sorted(set(member_ids or ()) | set(current_member_ids))
    if member_ids:
        stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
        advs = db_manager.query(AdventurerS1).options(*([noload('*')] if match_error is not None else [])).populate_existing().filter(AdventurerS1.id.in_(member_ids)).with_for_update().all()
        for adv in advs:
            if adv.status not in ('in_party', 'exploring'):
                continue
            if exploring_stamina_refund and adv.status == 'exploring':
                adv.stamina = min(stamina_max, int(adv.stamina or 0) + exploring_stamina_refund)
            AdventurerS1StateTransitionCoordinator.enter_rest(adv)
    db_manager.query(AdventurerS1PartyMember).filter_by(party_id=party_id).delete(synchronize_session=False)
    _clear_departure_votes(party_id)
    now = cancelled_at or _portable_utc_now()
    matched_tickets = db_manager.query(AdventurerS1MatchTicket).options(*([noload('*')] if match_error is not None else [])).filter_by(matched_party_id=party_id, status='matched').all()
    for ticket in matched_tickets:
        ticket.status = 'cancelled'
        ticket.cancelled_at = ticket.cancelled_at or now
        if match_error is not None:
            ticket.match_error = match_error
            ticket.queued_at = None
        if campaign_task_cooldown_until is not None:
            ticket.campaign_task_cooldown_until = campaign_task_cooldown_until
    if match_error is not None and matched_tickets:
        from .adventurer_s1_matchmaking_service import _cancel_pending_invites
        _cancel_pending_invites(ticket_ids=[t.id for t in matched_tickets])
    party.status = 'disbanded'
    db_manager.query(AdventurerS1SpecialPartySlot).filter_by(party_id=party.id).delete(synchronize_session=False)
