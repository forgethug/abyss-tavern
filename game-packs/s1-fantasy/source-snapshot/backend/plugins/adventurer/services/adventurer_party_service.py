from runtime_core.time import utc_now as _portable_utc_now
import copy
import random
from datetime import datetime, timedelta
from flask import current_app

__all__ = [
    'generate_random_adventurers',
    'ensure_min_adventurers',
    'form_parties',
]

from db_repository.manager import db_manager
from plugins.adventurer.models import Adventurer, AdventurerParty, PartyMember
from models.user import User
from plugins.adventurer.services.adventurer_config_service import (
    CLASSES,
    DEFAULT_SKILLS,
    DUNGEON_RANK_MAP,
    DUNGEONS,
    FEMALE_NAMES,
    LAST_NAMES,
    MALE_NAMES,
    PERSONALITIES,
    _find_same_rank_forming_party,
    _get_adventurer_real_rank,
    _get_dynamic_max_forming_parties,
    _pick_weighted_dungeon_for_adventurer,
    get_auto_party_settings,
    get_max_adventurers,
)


def generate_random_adventurers(count=5):
    """随机生成指定数量的冒险者。仅当存活人数小于10时，才会自动生成补齐到10人"""
    current_alive = db_manager.adventurer.get_alive_count()
    max_limit = get_max_adventurers()
    if current_alive >= 10:
        return []

    actual_count = max(0, min(count, 10 - current_alive))
    actual_count = max(0, min(actual_count, max_limit - current_alive))

    if actual_count <= 0:
        return []

    adventurers = []

    pool_3_days = []
    pool_7_days = []
    pool_all = []
    try:
        user_records = db_manager.query(User.nickname, User.username, User.last_login_at).filter(User.is_deleted == False).all()
        existing_adv_names = set([row[0] for row in db_manager.query(Adventurer.name).all()])
        now = _portable_utc_now()

        for nickname, username, last_login_at in user_records:
            if not nickname:
                continue

            name = "".join(c for c in nickname if c.isprintable()).strip()

            if not name or name in existing_adv_names or name.lower() == 'null':
                continue

            if last_login_at:
                days_since_login = (now - last_login_at).days
                if days_since_login <= 3:
                    pool_3_days.append(name)
                elif days_since_login <= 7:
                    pool_7_days.append(name)
                else:
                    pool_all.append(name)
            else:
                pool_all.append(name)
    except Exception as e:
        current_app.logger.warning(f"无法获取玩家列表作为名字池: {e}")

    for _ in range(actual_count):
        hp_val = random.randint(80, 150)
        gender = random.choice(['男', '女'])

        if pool_3_days:
            adv_name = random.choice(pool_3_days)
            pool_3_days.remove(adv_name)
        elif pool_7_days:
            adv_name = random.choice(pool_7_days)
            pool_7_days.remove(adv_name)
        elif pool_all:
            adv_name = random.choice(pool_all)
            pool_all.remove(adv_name)
        else:
            if gender == '男':
                first_name = random.choice(MALE_NAMES)
            else:
                first_name = random.choice(FEMALE_NAMES)
            last_name = random.choice(LAST_NAMES)
            adv_name = f"{first_name}{last_name}"

        char_class = random.choice(CLASSES)
        initial_skills = copy.deepcopy(DEFAULT_SKILLS.get(char_class, []))

        adv = Adventurer(
            name=adv_name,
            gender=gender,
            character_class=char_class,
            level=1,
            hp=hp_val,
            max_hp=hp_val,
            strength=random.randint(5, 20),
            agility=random.randint(5, 20),
            intelligence=random.randint(5, 20),
            luck=random.randint(1, 10),
            personality=",".join(random.sample(PERSONALITIES, k=random.randint(1, 2))),
            equipment={"铁剑": {"type": "weapon", "desc": "普通的铁制长剑"}, "布甲": {"type": "armor", "desc": "基础的布制防具"}},
            skills=initial_skills
        )
        adv.max_hp = adv.hp
        adv.gold = random.uniform(50000, 200000)
        adventurers.append(adv)

    with db_manager.transaction():
        for adv in adventurers:
            db_manager.add(adv)

    return adventurers


def ensure_min_adventurers():
    """确保系统中有足够数量的存活且空闲冒险者。闭馆期间跳过自动补齐。"""
    from services.adventurer_closure_service import get_closure_state, STATE_PENDING
    closure = get_closure_state()
    if closure.get('state') in (STATE_PENDING, 'running_defense', 'closed'):
        return
    idle_count = db_manager.adventurer.get_alive_count()
    if idle_count < 10:
        generate_random_adventurers(10 - idle_count)


def form_parties(force_auto_party_enabled: bool = False):
    """让空闲的冒险者随机加入已有的集结中小队，或者创建新小队。

    force_auto_party_enabled=True 仅供 GM 在闭馆本地测试时手动触发一轮
    正式版自动组队逻辑使用：它会临时放开“自动新建小队已关闭”的限制，但
    仍然沿用当前的 max_forming_parties、概率分布和闭馆期不自动补新冒险者的规则。
    """
    changed_adventurer_ids = set()
    changed_party_ids = set()
    with db_manager.transaction():
        now = _portable_utc_now()
        timeout_threshold = now - timedelta(hours=1)
        overtime_adventurers = db_manager.query(Adventurer).filter(
            Adventurer.party_preference == 'rest',
            Adventurer.rest_start_time != None,
            Adventurer.rest_start_time <= timeout_threshold
        ).all()
        for adv in overtime_adventurers:
            adv.party_preference = 'auto'
            changed_adventurer_ids.add(adv.id)

    ensure_min_adventurers()

    auto_party_settings = get_auto_party_settings()
    auto_party_enabled = auto_party_settings['auto_party_enabled'] or force_auto_party_enabled

    idle_adventurers = db_manager.adventurer.get_for_party()
    random.shuffle(idle_adventurers)

    parties_created = []

    forming_parties = db_manager.adventurer_party.get_forming()

    party_member_counts = {p.id: len(p.members) for p in forming_parties}

    manual_limit_enabled = auto_party_settings['manual_limit_enabled']
    max_forming_parties = (
        auto_party_settings['max_forming_parties']
        if manual_limit_enabled
        else _get_dynamic_max_forming_parties(len(idle_adventurers))
    )

    remaining_idle_count = len(idle_adventurers)

    with db_manager.transaction():
        for adv in idle_adventurers:
            if random.random() < 0.4:
                continue

            chosen_party = None

            adv_real_rank = _get_adventurer_real_rank(adv)

            if forming_parties and random.random() < 0.7:
                available_forming_parties = [p for p in forming_parties if party_member_counts.get(p.id, 0) < p.target_size]

                if available_forming_parties:
                    willing_parties = []
                    for p in available_forming_parties:
                        target_rank = DUNGEON_RANK_MAP.get(p.target_dungeon, 0)

                        if adv.level == 1 and target_rank > 0:
                            continue

                        if target_rank > adv_real_rank:
                            rank_diff = target_rank - adv_real_rank
                            is_higher = True
                        elif target_rank < adv_real_rank:
                            rank_diff = adv_real_rank - target_rank
                            is_higher = False
                        else:
                            rank_diff = 0
                            is_higher = None

                        # 副本相对冒险者：比自己高最多 +1 档，比自己低最多 -2 档
                        if is_higher is True and rank_diff > 1:
                            continue
                        if is_higher is False and rank_diff > 2:
                            continue

                        if rank_diff == 0:
                            join_probability = 0.90
                        elif is_higher:
                            join_probability = 0.50
                        elif rank_diff == 1:
                            join_probability = 0.60
                        else:
                            join_probability = 0.40

                        if random.random() > join_probability:
                            continue

                        willing_parties.append(p)

                    if willing_parties:
                        chosen_party = random.choice(willing_parties)

            if not chosen_party and auto_party_enabled and random.random() < 0.15:
                needed_members_for_existing = sum(
                    p.target_size - party_member_counts.get(p.id, 0)
                    for p in forming_parties
                )
                if (remaining_idle_count - needed_members_for_existing) >= 10:
                    chosen_dungeon_name = _pick_weighted_dungeon_for_adventurer(adv, adv_real_rank)
                    chosen_dungeon_rank = DUNGEON_RANK_MAP.get(chosen_dungeon_name, 0)

                    existing_same_rank_party = _find_same_rank_forming_party(
                        forming_parties=forming_parties,
                        party_member_counts=party_member_counts,
                        target_rank=chosen_dungeon_rank
                    )

                    if existing_same_rank_party:
                        chosen_party = existing_same_rank_party
                    elif len(forming_parties) < max_forming_parties:
                        chosen_party = AdventurerParty(
                            name=f"小队_{random.randint(100, 999)}",
                            target_dungeon=chosen_dungeon_name,
                            target_size=random.randint(4, 8),
                            status='forming',
                            leader_id=adv.id
                        )
                        db_manager.add(chosen_party)
                        db_manager.flush()
                        forming_parties.append(chosen_party)
                        parties_created.append(chosen_party)
                        changed_party_ids.add(chosen_party.id)
                        party_member_counts[chosen_party.id] = 0

            if chosen_party:
                adv.status = 'in_party'
                changed_adventurer_ids.add(adv.id)
                changed_party_ids.add(chosen_party.id)
                db_manager.flush()
                pm = PartyMember(party_id=chosen_party.id, adventurer_id=adv.id)
                db_manager.add(pm)

                remaining_idle_count -= 1

                party_member_counts[chosen_party.id] = party_member_counts.get(chosen_party.id, 0) + 1

                if party_member_counts[chosen_party.id] >= chosen_party.target_size:
                    if not chosen_party.departure_time:
                        reduced_mins = getattr(chosen_party, 'sponsor_reduced_minutes', 0)
                        chosen_party.departure_time = _portable_utc_now() + timedelta(minutes=45) - timedelta(minutes=reduced_mins)
                        changed_party_ids.add(chosen_party.id)

    if changed_adventurer_ids or changed_party_ids:
        from plugins.adventurer.services.adventurer_log_service import emit_tavern_precise_updates
        emit_tavern_precise_updates(
            adventurer_ids=list(changed_adventurer_ids),
            party_ids=list(changed_party_ids),
            reason='party_formed'
        )
    return parties_created


def _count_forming_party_members() -> int:
    """统计所有 forming 小队中的成员总数，用于 GM 手动触发后的反馈摘要。"""
    return db_manager.query(PartyMember.id).join(
        AdventurerParty,
        PartyMember.party_id == AdventurerParty.id
    ).filter(
        AdventurerParty.status == 'forming'
    ).count()


def run_auto_party_once() -> tuple[dict | None, str | None]:
    """GM 手动执行一轮正式版自动组队逻辑，复用 `form_parties()`。"""
    settings = get_auto_party_settings()
    forming_before = len(db_manager.adventurer_party.get_forming())
    idle_before = db_manager.adventurer.count(status='idle')
    forming_member_count_before = _count_forming_party_members()

    created_parties = form_parties()

    forming_after = len(db_manager.adventurer_party.get_forming())
    idle_after = db_manager.adventurer.count(status='idle')
    forming_member_count_after = _count_forming_party_members()

    created_party_payloads = [
        {
            'id': int(p.id),
            'name': p.name,
            'target_dungeon': p.target_dungeon,
            'target_size': int(p.target_size or 0),
        }
        for p in created_parties
    ]
    joined_count = max(forming_member_count_after - forming_member_count_before, 0)
    created_count = len(created_party_payloads)

    if created_count or joined_count:
        message = (
            f'已执行一轮自动组队：新建 {created_count} 支小队，'
            f'新增 {joined_count} 名成员进入集结'
        )
    elif not settings['auto_party_enabled']:
        message = '已执行一轮自动组队：自动新建小队当前关闭，本轮没有新的组队变化'
    else:
        message = '已执行一轮自动组队：本轮没有形成新的组队变化'

    return {
        'auto_party_enabled': settings['auto_party_enabled'],
        'manual_limit_enabled': settings['manual_limit_enabled'],
        'max_forming_parties': settings['max_forming_parties'],
        'forming_parties_before': forming_before,
        'forming_parties_after': forming_after,
        'idle_before': idle_before,
        'idle_after': idle_after,
        'joined_count': joined_count,
        'created_party_count': created_count,
        'created_parties': created_party_payloads,
        'message': message,
    }, None


def run_closure_auto_party_once() -> tuple[dict | None, str | None]:
    """闭馆 pending 期间，同步真实的归隐/守护集结队伍。"""
    from services.adventurer_closure_runner import sync_closure_forming_parties

    return sync_closure_forming_parties()
