"""深渊酒馆 S1 初始种子数据：可选初始技能模板 + 初始武器。

设计取向：技能/武器都不绑定职业（S1 已取消创建期职业），描述写成"AI 跑团时
好用的叙事钩子"——点明定位、擅长场景与代价，方便主探险 AI 据此推进剧情与结算，
而不是给一堆纯数值。所有内容均为入门档（common 为主，少量 rare）。

种子是幂等的：按名称判重，已存在则跳过，可安全重复调用（启动、赛季重置后均会调用）。
"""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime, timedelta
import re

from flask import current_app

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1SkillTemplate, AdventurerS1Item, AdventurerS1DormRoom, AdventurerS1BuildingTemplate, AdventurerS1BuildingInstance, AdventurerS1TavernSeat, AdventurerS1TeaHouseSeat, AdventurerS1HotSpringSeat, AdventurerS1Equipment, AdventurerS1WarehouseItem, AdventurerS1MarketListing, AdventurerS1CurrencyLog, AdventurerS1LuckyWheelSpin, _BUILDING_ADMIN_GATED_DONATION_CODES
from plugins.adventurer_s1.services.s1_title_service import seed_s1_titles
from plugins.adventurer_s1.services.s1_fate_companion_service import (
    backfill_fate_companion_credits,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import (
    normalize_item_instance_name_and_quantity,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_HIDDEN_DUNGEONS
from plugins.adventurer_s1.s1_item_types import (
    S1_ARMORY_STARTER_TYPES,
    S1_CONSUMABLE_ITEM_TYPES,
)


def _commit_building_seed_or_raise(operation: str) -> None:
    """提交建筑种子写入；失败时中止后续 seed，避免留下半初始化状态。"""
    ok, err = db_manager.commit()
    if not ok:
        raise RuntimeError(f'S1 建筑种子{operation}提交失败：{err or "未知数据库错误"}')


# 12 个初始技能模板：(名称, 描述)
SKILL_TEMPLATES = [
    ('精准射击', '锁定要害的远程一击，擅长在敌人靠近前造成致命打击；需要清晰视野与拉开的距离，混战贴脸时会大打折扣。'),
    ('重盾格挡', '用盾牌硬抗正面冲击，为自己和身后队友挡下致命攻击；面对范围打击或背袭时保护有限，长时间格挡也会消耗体力。'),
    ('治愈之触', '施展温和的治愈之力，包扎伤口、止血续命；对中毒、诅咒等异常无效，重伤回复缓慢且消耗施术者的精神。'),
    ('烈焰迸发', '引爆一团烈焰灼烧前方范围内的敌人，对成群弱敌与易燃目标极有效；狭窄空间使用可能殃及队友，对火抗目标几乎无用。'),
    ('暗影潜行', '隐去身形悄然移动，可规避危险、侦察或发动突袭；一旦被察觉或主动出手便会显形，强光与广域感知会让它失效。'),
    ('战阵怒吼', '以气势鼓舞全队、震慑敌人，短时间提振士气、压制弱小目标的行动；对意志坚定或无心智的敌人收效甚微。'),
    ('净化祝祷', '驱散毒素、瘴气与轻度诅咒等负面状态，让中招的队友重新站起；对重度诅咒、终局级反噬只能缓解，无法根除。'),
    ('机关拆解', '识破并拆除陷阱、暗锁与机关，为队伍开路、避开伏击；操作需要时间与专注，慌乱中强行拆解可能触发反噬。'),
    ('野外求生', '在恶劣环境中辨路、觅食、寻水与搭建临时庇护，延长队伍的探险续航；面对超自然环境或资源彻底枯竭时无能为力。'),
    ('心眼洞察', '看破敌人的弱点、动向与谎言，为队伍提供关键判断与先手；信息越复杂越耗神，被高阶存在蒙蔽时也可能误判。'),
    ('疾风步法', '以极快的步法拉开或缩短距离，追击、脱离接触或抢占要点；持续高速移动消耗巨大，地形受限时难以施展。'),
    ('守护结界', '张开一道护盾结界，短时间内为范围内同伴抵御伤害；结界有承伤上限，被持续猛攻会迅速碎裂，重张需要间隔。'),
    # 技能书阁可学技能（对应 _DEFAULT_CATALOG 中 gold_cost > 0 的条目）
    ('瞄准射击', '屏息瞄准远处目标的要害精准一箭，射程远、单体伤害高；需要稳定站位与清晰视野，贴身混战或剧烈晃动时几乎无法命中。'),
    ('照明术', '凝聚光元素照亮周围大片区域，驱散黑暗与迷雾、暴露隐藏的敌人和机关；对光抗性极高的暗影生物几乎无效，持续时间有限。'),
    ('惩击', '凝聚神圣之力打击单个目标，对亡灵与邪魔有额外震慑效果；对普通生物伤害平平，施术者精神不集中时容易打偏。'),
    ('陷阱布置', '就地取材布设绊索、落石或尖刺陷阱，诱敌深入后触发伤害或限制行动；需要时间与隐蔽空间，被发现后陷阱会被绕开或拆除。'),
]


# 17 件初始装备：(名称, 类型, 描述, 稀有度)
# 5 武器 + 4 盾牌 + 4 防具 + 4 特殊用品，描述侧重"AI 跑团时的叙事钩子"——
# 点明擅长场景与代价，方便主探险 AI 据此决定战斗/探索如何展开。
# 其中武器/盾牌/防具同步上架军需店（见 S1_ARMORY_ITEMS），供装备损坏后补购。
STARTER_ITEMS = [
    # ── 武器 ──
    ('新兵铁剑', 'weapon', '军中制式的朴素铁剑，平衡可靠、好上手，是大多数新人冒险者的起点；威力平平，面对重甲与巨物略显吃力。', 'common'),
    ('猎人短弓', 'weapon', '轻便的木制短弓，适合远程狩猎与游走输出；近身缠斗时几乎无法发挥，箭矢耗尽便成摆设。', 'common'),
    ('伐木巨斧', 'weapon', '由伐木巨斧改制的双手重武器，一击威力惊人、擅长破甲；挥砍笨重、破绽大，灵活的对手很难命中。', 'common'),
    ('双生匕首', 'weapon', '一对轻盈匕首，适合快速连击、背刺与脱战；单击伤害低，正面硬拼极易吃亏。', 'common'),
    ('学徒法杖', 'weapon', '白蜡木削制的朴素法杖，杖首嵌着一颗劣质魔晶，能引导施法者的精神力汇聚成型；近身搏斗毫无用处，一旦魔晶碎裂便与普通木棍无异。', 'common'),
    # ── 盾牌 ──
    ('步兵圆盾', 'shield', '轻便的圆形盾牌，可灵活格挡、不影响移动，适合前排战士稳住战线；面积有限，挡不住大范围冲击。', 'common'),
    ('守卫塔盾', 'shield', '厚重的全身塔盾，能在关键时刻筑起一道临时防线、护住身后同伴；极其沉重，持盾者近乎丧失机动。', 'common'),
    ('刺客小圆盾', 'shield', '巴掌大的金属小圆盾，擅长弹反与招架、配合匕首使用者的快速反应；几乎挡不住箭矢与范围攻击。', 'common'),
    ('荆棘藤盾', 'shield', '用浸过树脂的荆棘藤编织的盾，表面带刺，近战攻击者容易被反伤；怕火，遇烈焰会迅速燃烧殆尽。', 'common'),
    # ── 防具 ──
    ('皮革轻甲', 'armor', '鞣制兽皮制成的轻甲，不阻碍行动、适合潜行与敏捷型冒险者；对穿刺与钝击的防护十分有限。', 'common'),
    ('链甲衫', 'armor', '细密铁环编成的链甲，有效抵御劈砍，是最常见的中等防护；沉重闷热，长途跋涉会大量消耗体力。', 'common'),
    ('学徒法袍', 'armor', '绣着基础防护符文的布袍，能引导魔力流通、提升施法手感；物理防御几乎为零，一刀就能划破。', 'common'),
    ('斥候轻装', 'armor', '灰绿拼色的紧身衣裤，融入林地与暗影、大幅降低被发现的概率；没有实质防护，被命中就只能硬扛。', 'common'),
    # ── 特殊用品 ──
    ('急救药包', 'special', '装着绷带、止血粉与一小瓶治愈药水的急救包，能在战斗间隙快速处理伤口；用完即弃，且无法治愈重伤或异常。', 'common'),
    ('攀爬绳钩', 'special', '一卷结实的麻绳配铁爪钩，能翻越断崖、攀上高墙、横渡深沟；需要一定臂力与技巧，慌乱中使用容易脱手。', 'common'),
    ('旧地下城地图', 'special', '一张褪色的羊皮地图，标注着部分通道与密室位置，可能指引捷径或宝藏；信息残缺且可能过时，盲信反而会迷路。', 'common'),
    ('护身符', 'special', '刻着古神名讳的铜片护身符，佩戴时隐约感到被庇佑、厄运似乎少了些许；效果微妙且不可验证，也许只是心理安慰。', 'common'),
]


def seed_skill_templates() -> int:
    """补齐缺失的初始技能模板，返回本次新增条数。"""
    existing = {t.name for t in AdventurerS1SkillTemplate.query.all()}
    to_add = [
        AdventurerS1SkillTemplate(name=name, description=desc)
        for name, desc in SKILL_TEMPLATES
        if name not in existing
    ]
    if to_add:
        db_manager.add_all(to_add, flush=False)
        db_manager.commit()
    return len(to_add)


def seed_starter_items() -> int:
    """补齐缺失的初始装备（写入图鉴，标记 is_starter=True），返回本次新增条数。"""
    names = [w[0] for w in STARTER_ITEMS]
    existing = {
        i.name: i
        for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    for name, type_, desc, rarity in STARTER_ITEMS:
        item = existing.get(name)
        if item is None:
            to_add.append(AdventurerS1Item(
                name=name, type=type_, description=desc, rarity=rarity, is_starter=True,
            ))
        else:
            if not item.is_starter:
                item.is_starter = True
            if item.rarity != rarity:
                item.rarity = rarity
            # 更新类型（旧数据全是 weapon，需要修正为 shield/armor/special）
            if item.type != type_:
                item.type = type_
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def seed_hidden_dungeon_drop_items() -> int:
    """幂等补齐隐藏副本专属掉落；这些都是可交易、锻造和消耗的普通图鉴物品。"""
    definitions = [
        drop
        for hidden in S1_HIDDEN_DUNGEONS
        for drop in (hidden.get('drops') or [])
    ]
    names = [str(drop.get('item_name') or '').strip() for drop in definitions]
    existing = {
        item.name: item
        for item in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    for drop in definitions:
        name = str(drop.get('item_name') or '').strip()
        if not name or name in existing:
            continue
        to_add.append(AdventurerS1Item(
            name=name,
            type=str(drop.get('type') or 'material'),
            description=str(drop.get('description') or ''),
            rarity=str(drop.get('rarity') or 'common'),
            is_starter=False,
            world_unique=False,
        ))
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def seed_independent_material_items() -> int:
    """仅补齐预设材料图鉴；已有条目和玩家资产不被启动过程覆盖。"""
    from .services.s1_independent_material_catalog import material_pool_snapshot
    definitions = material_pool_snapshot()
    names = [row['name'] for row in definitions]
    existing = {
        row[0] for row in AdventurerS1Item.query.with_entities(AdventurerS1Item.name)
        .filter(AdventurerS1Item.name.in_(names)).all()
    }
    rows = [
        AdventurerS1Item(
            name=row['name'], type='material', rarity=row['rarity'],
            description=row['desc'], is_starter=False, world_unique=False,
        )
        for row in definitions if row['name'] not in existing
    ]
    if rows:
        db_manager.add_all(rows, flush=False)
    db_manager.commit()
    return len(rows)


def backfill_tombstones() -> int:
    """为已阵亡但尚无墓碑的冒险者补登记。"""
    from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService
    return S1TombstoneService.backfill_missing_tombstones()


def backfill_dead_adventurer_confiscations() -> int:
    """没收历史死亡冒险者残留资产，避免市场/材料继续占在旧角色名下。"""
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    return AdventurerS1Service.backfill_dead_adventurer_confiscations()





def seed_limited_task_defaults() -> int:
    """幂等设置限时任务默认禁用；已有覆盖时不覆盖管理员手动操作。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride

    added = 0
    for task_id in ('hidden_boss_hunt', 'hidden_boss_hunt_task_v2', 'goblin_mine_treasure_hunt'):
        override = AdventurerS1TaskOverride.query.filter_by(task_template_id=task_id).first()
        if override:
            continue
        db_manager.add(AdventurerS1TaskOverride(
            task_template_id=task_id,
            enabled=False,
        ))
        added += 1
    ok, err = db_manager.commit()
    if not ok:
        raise RuntimeError(err or '设置限时任务默认禁用失败')
    return added


def seed_hidden_boss_task_default() -> int:
    """兼容旧调用：设置限时任务默认禁用。"""
    return seed_limited_task_defaults()


def repair_expedition_residual_codenames() -> int:
    """显式维护入口：增量修复历史探险代号，不得在应用启动链路调用。"""
    from flask import current_app
    from plugins.adventurer_s1.services.adventurer_s1_expedition_codename_repair_service import (
        AdventurerS1ExpeditionCodenameRepairService,
    )

    try:
        return AdventurerS1ExpeditionCodenameRepairService.repair_startup_incremental()
    except Exception:  # noqa: BLE001
        current_app.logger.exception('S1 探险代号增量修复失败')
        return 0


def seed_reconcile_daily_reset_tasks() -> int:
    """启动时收敛每日重置任务：停用旧 __d/__r 派生版本，确保根模板可见。"""
    from flask import current_app
    from sqlalchemy import or_
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride
    from plugins.adventurer_s1.services.adventurer_s1_task_service import (
        _DAILY_RESET_SOURCE_IDS,
        _invalidate_overrides_cache,
    )

    changed = 0
    try:
        with db_manager.transaction():
            for source_id in _DAILY_RESET_SOURCE_IDS:
                family = AdventurerS1TaskOverride.query.filter(or_(
                    AdventurerS1TaskOverride.task_template_id == source_id,
                    AdventurerS1TaskOverride.source_template_id == source_id,
                    AdventurerS1TaskOverride.task_template_id.like(f'{source_id}__d%'),
                    AdventurerS1TaskOverride.task_template_id.like(f'{source_id}__r%'),
                )).all()
                root = None
                for row in family:
                    if row.task_template_id == source_id:
                        root = row
                    elif row.enabled:
                        row.enabled = False
                        changed += 1
                if root is not None and not root.enabled:
                    root.enabled = True
                    changed += 1
        if changed:
            _invalidate_overrides_cache()
        ok, commit_err = db_manager.commit()
        if not ok:
            current_app.logger.warning('S1 每日重置任务收敛提交失败: %s', commit_err)
            return 0
        return changed
    except Exception:  # noqa: BLE001
        current_app.logger.exception('S1 每日重置任务收敛异常')
        return 0


def seed_all() -> dict:
    """一次性补齐技能模板与初始装备，返回各自新增条数。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_log_member_service import (
        backfill_expedition_log_members,
    )

    titles_added = seed_s1_titles()
    expedition_log_members_backfilled = backfill_expedition_log_members(
        max_batches=40,
        skip_when_fully_indexed=True,
    )
    fate_companion_credits_added = backfill_fate_companion_credits()
    return {
        'titles_added': titles_added,
        'expedition_log_members_backfilled': expedition_log_members_backfilled,
        'fate_companion_credits_added': fate_companion_credits_added,
        'skill_templates_added': seed_skill_templates(),
        'starter_items_added': seed_starter_items(),
        'hidden_dungeon_drop_items_added': seed_hidden_dungeon_drop_items(),
        'independent_material_items_added': seed_independent_material_items(),
        'dorm_rooms_added': seed_dorm_rooms(),
        'building_templates_added': seed_building_templates(),
        'building_templates_retired': deactivate_retired_building_templates(),
        'lv2_orphan_instances_purged': _purge_orphan_lv2_locked_instances(),
        'building_instances_added': seed_building_instances(),
        'building_instances_synced': sync_building_instance_states(),
        'herbalist_shop_items_added': seed_herbalist_shop_items(),
        'armory_shop_items_added': seed_armory_shop_items(),
        'forge_supply_shop_items_added': seed_forge_supply_shop_items(),
        'orphan_shop_listings_removed': cleanup_orphan_shop_listings(),
        'currencies_seeded': seed_s1_currencies(),
        'currencies_migrated_to_materials': migrate_currencies_to_materials(),
        'gold_rail_synced': sync_gold_currency_with_main(),
        'tavern_seats_added': seed_tavern_seats(),
        'tea_house_seats_added': seed_tea_house_seats(),
        'hot_spring_seats_added': seed_hot_spring_seats(),
        'dining_menu_items_added': seed_dining_menu_items(),
        'task_catalog_items_added': seed_task_catalog_items(),
        'special_dungeon_ticket_items_added': seed_special_dungeon_ticket_items(),
        'lucky_wheel_catalog_items_added': seed_lucky_wheel_catalog_items(),
        'class_transfer_exchange_items_added': seed_class_transfer_exchange_catalog_items(),
        'lucky_wheel_history_backfilled': backfill_lucky_wheel_history(),
        'tombstones_backfilled': backfill_tombstones(),
        'dead_adventurer_assets_confiscated': backfill_dead_adventurer_confiscations(),
        'limited_task_defaults_disabled': seed_limited_task_defaults(),
        'hidden_boss_clue_items_synced': seed_hidden_boss_clue_items(),
        'legacy_item_stacks_normalized': normalize_legacy_item_stacks(),
        # 历史正文扫描不能阻塞持有初始化锁的 worker；保留统计键兼容调用方。
        # 需要修复时显式调用 repair_expedition_residual_codenames，启动不推进检查点。
        'expedition_codenames_repaired': 0,
        'daily_reset_tasks_reconciled': seed_reconcile_daily_reset_tasks(),
    }


_LUCKY_WHEEL_RESULT_PATTERN = re.compile(r'抽到「(.+?)」')


def backfill_lucky_wheel_history(batch_size: int = 500) -> int:
    """分批回填能从旧金币流水中明确识别奖品的大转盘历史。"""
    migrated = 0
    last_id = 0
    while True:
        logs = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.id > last_id,
                AdventurerS1CurrencyLog.source_type == 'lucky_wheel_spin',
                AdventurerS1CurrencyLog.description.like('%抽到「%'),
            )
            .order_by(AdventurerS1CurrencyLog.id.asc())
            .limit(batch_size)
            .all()
        )
        if not logs:
            break
        last_id = logs[-1].id
        log_ids = [log.id for log in logs]
        existing_log_ids = {
            row[0] for row in (
                AdventurerS1LuckyWheelSpin.query
                .with_entities(AdventurerS1LuckyWheelSpin.source_currency_log_id)
                .filter(AdventurerS1LuckyWheelSpin.source_currency_log_id.in_(log_ids))
                .all()
            )
        }
        parsed: list[tuple] = []
        item_names: set[str] = set()
        for log in logs:
            if log.id in existing_log_ids:
                continue
            match = _LUCKY_WHEEL_RESULT_PATTERN.search(log.description or '')
            try:
                building_instance_id = int(log.related_id)
            except (TypeError, ValueError):
                continue
            if not match:
                continue
            item_name = match.group(1)
            parsed.append((log, building_instance_id, item_name))
            item_names.add(item_name)

        rarity_by_name = {
            row.name: row.rarity
            for row in (
                AdventurerS1Item.query
                .with_entities(AdventurerS1Item.name, AdventurerS1Item.rarity)
                .filter(AdventurerS1Item.name.in_(item_names))
                .all()
            )
        } if item_names else {}
        unlinked_by_key: dict[tuple, list[AdventurerS1LuckyWheelSpin]] = {}
        if parsed:
            created_times = [log.created_at for log, _, _ in parsed]
            adventurer_ids = {log.adventurer_id for log, _, _ in parsed}
            building_ids = {building_id for _, building_id, _ in parsed}
            unlinked_rows = (
                AdventurerS1LuckyWheelSpin.query
                .filter(
                    AdventurerS1LuckyWheelSpin.source_currency_log_id.is_(None),
                    AdventurerS1LuckyWheelSpin.payment == 'gold',
                    AdventurerS1LuckyWheelSpin.adventurer_id.in_(adventurer_ids),
                    AdventurerS1LuckyWheelSpin.building_instance_id.in_(building_ids),
                    AdventurerS1LuckyWheelSpin.created_at >= min(created_times) - timedelta(seconds=5),
                    AdventurerS1LuckyWheelSpin.created_at <= max(created_times) + timedelta(seconds=5),
                )
                .all()
            )
            for row in unlinked_rows:
                key = (row.adventurer_id, row.building_instance_id, row.item_name)
                unlinked_by_key.setdefault(key, []).append(row)

        rows = []
        linked_existing = 0
        for log, building_instance_id, item_name in parsed:
            key = (log.adventurer_id, building_instance_id, item_name)
            candidates = unlinked_by_key.get(key, [])
            matching = next(
                (
                    row for row in candidates
                    if abs((row.created_at - log.created_at).total_seconds()) <= 5
                ),
                None,
            )
            if matching is not None:
                matching.source_currency_log_id = log.id
                candidates.remove(matching)
                linked_existing += 1
                continue
            rows.append(AdventurerS1LuckyWheelSpin(
                adventurer_id=log.adventurer_id,
                building_instance_id=building_instance_id,
                source_currency_log_id=log.id,
                item_name=item_name,
                item_rarity=rarity_by_name.get(item_name, 'common'),
                payment='gold',
                gold_cost=int(abs(float(log.change_amount or 0))),
                created_at=log.created_at,
                updated_at=log.created_at,
            ))
        if rows or linked_existing:
            ok, err = db_manager.add_all(rows, flush=False)
            if not ok:
                raise RuntimeError(err or '大转盘旧历史回填失败')
            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '大转盘旧历史回填提交失败')
            migrated += len(rows) + linked_existing
    return migrated


def normalize_legacy_item_stacks() -> int:
    """Move strict trailing name quantity suffixes into the quantity column."""
    changed = 0
    for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
        last_id = 0
        while True:
            rows = (
                model.query
                .filter(model.id > last_id)
                .order_by(model.id.asc())
                .limit(500)
                .all()
            )
            if not rows:
                break
            for row in rows:
                last_id = row.id
                if normalize_item_instance_name_and_quantity(row):
                    changed += 1
            if changed:
                db_manager.commit()

    last_id = 0
    while True:
        listings = (
            AdventurerS1MarketListing.query
            .filter(AdventurerS1MarketListing.id > last_id)
            .order_by(AdventurerS1MarketListing.id.asc())
            .limit(500)
            .all()
        )
        if not listings:
            break
        for listing in listings:
            last_id = listing.id
            original_name = listing.item_name
            listing.name = listing.item_name
            if normalize_item_instance_name_and_quantity(listing):
                listing.item_name = listing.name
                changed += 1
            if hasattr(listing, 'name'):
                delattr(listing, 'name')
            if listing.item_name != original_name:
                changed += 0
        if changed:
            db_manager.commit()
    return changed


# ── 宿舍房间种子 ──────────────────────────────────────────────────────────

DORM_ROOMS = [
    ('4人间 A', 4),
    ('4人间 B', 4),
    ('4人间 C', 4),
    ('4人间 D', 4),
    ('2人间 A', 2),
    ('2人间 B', 2),
    ('2人间 C', 2),
    ('2人间 D', 2),
    ('单人间 A', 1),
    ('单人间 B', 1),
    ('单人间 C', 1),
    ('单人间 D', 1),
]


def seed_dorm_rooms() -> int:
    '初始化普通宿舍，返回本次新增条数。\n\n    房间名称允许玩家修改，不能把可变名称当作 seed 身份。只要已有任意宿舍，\n    就说明本赛季已经完成普通宿舍初始化；后续新增普通宿舍只能走玩家建造流程。\n    \n    '
    to_add = []
    changed = False
    regular_room_exists = AdventurerS1DormRoom.query.filter(
        AdventurerS1DormRoom.room_key.is_(None),
    ).first() is not None
    if not regular_room_exists:
        to_add.extend(
            AdventurerS1DormRoom(name=name, capacity=cap, level=1)
            for name, cap in DORM_ROOMS
        )


    if to_add:
        db_manager.add_all(to_add, flush=False)
    if to_add or changed:
        db_manager.commit()
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_dormitory_cache,
        )
        invalidate_dormitory_cache()
    return len(to_add)


# ── 酒馆大堂座位种子 ──────────────────────────────────────────────────────

# (seat_key, zone, table_key, table_label, label)
# 吧台单人座 ×6 + 双人桌 ×2 + 四人圆桌 ×2 = 18 个座位槽。
# 同 table_key 的座位在前端画成一桌；seat_key 为幂等判重标识，不要改已上线的值。
TAVERN_SEATS = [
    ('bar_1', 'bar', None, None, '吧台 1 号'),
    ('bar_2', 'bar', None, None, '吧台 2 号'),
    ('bar_3', 'bar', None, None, '吧台 3 号'),
    ('bar_4', 'bar', None, None, '吧台 4 号'),
    ('bar_5', 'bar', None, None, '吧台 5 号'),
    ('bar_6', 'bar', None, None, '吧台 6 号'),
    ('table_a_1', 'table', 'table_a', '双人桌 A', '双人桌 A·1'),
    ('table_a_2', 'table', 'table_a', '双人桌 A', '双人桌 A·2'),
    ('table_b_1', 'table', 'table_b', '双人桌 B', '双人桌 B·1'),
    ('table_b_2', 'table', 'table_b', '双人桌 B', '双人桌 B·2'),
    ('table_c_1', 'table', 'table_c', '圆桌 C', '圆桌 C·1'),
    ('table_c_2', 'table', 'table_c', '圆桌 C', '圆桌 C·2'),
    ('table_c_3', 'table', 'table_c', '圆桌 C', '圆桌 C·3'),
    ('table_c_4', 'table', 'table_c', '圆桌 C', '圆桌 C·4'),
    ('table_d_1', 'table', 'table_d', '圆桌 D', '圆桌 D·1'),
    ('table_d_2', 'table', 'table_d', '圆桌 D', '圆桌 D·2'),
    ('table_d_3', 'table', 'table_d', '圆桌 D', '圆桌 D·3'),
    ('table_d_4', 'table', 'table_d', '圆桌 D', '圆桌 D·4'),
]


def seed_tavern_seats() -> int:
    """补齐缺失的酒馆座位，返回本次新增条数。按 seat_key 判重幂等。"""
    existing = {s.seat_key for s in AdventurerS1TavernSeat.query.all()}
    to_add = [
        AdventurerS1TavernSeat(
            seat_key=seat_key, zone=zone, table_key=table_key,
            table_label=table_label, label=label, sort_order=i,
        )
        for i, (seat_key, zone, table_key, table_label, label) in enumerate(TAVERN_SEATS)
        if seat_key not in existing
    ]
    if to_add:
        db_manager.add_all(to_add, flush=False)
        db_manager.commit()
    return len(to_add)


# ── 深渊茶舍座位种子 ──────────────────────────────────────────────────────────

# 六桌各四座 = 24 槽。
# tea_a/tea_b 为 Lv1 默认开放；tea_c/tea_d 需 Lv2；tea_e/tea_f 需 Lv3。
# seat_key 为幂等判重标识，不要改已上线的值。
TEA_HOUSE_SEATS = [
    ('tea_a_1', 'tea_a', '雅座·清风', '清风桌·1号'),
    ('tea_a_2', 'tea_a', '雅座·清风', '清风桌·2号'),
    ('tea_a_3', 'tea_a', '雅座·清风', '清风桌·3号'),
    ('tea_a_4', 'tea_a', '雅座·清风', '清风桌·4号'),
    ('tea_b_1', 'tea_b', '雅座·明月', '明月桌·1号'),
    ('tea_b_2', 'tea_b', '雅座·明月', '明月桌·2号'),
    ('tea_b_3', 'tea_b', '雅座·明月', '明月桌·3号'),
    ('tea_b_4', 'tea_b', '雅座·明月', '明月桌·4号'),
    # ── 以下两桌需茶舍 Lv2 解锁 ──
    ('tea_c_1', 'tea_c', '雅座·落花', '落花桌·1号'),
    ('tea_c_2', 'tea_c', '雅座·落花', '落花桌·2号'),
    ('tea_c_3', 'tea_c', '雅座·落花', '落花桌·3号'),
    ('tea_c_4', 'tea_c', '雅座·落花', '落花桌·4号'),
    ('tea_d_1', 'tea_d', '雅座·瑞雪', '瑞雪桌·1号'),
    ('tea_d_2', 'tea_d', '雅座·瑞雪', '瑞雪桌·2号'),
    ('tea_d_3', 'tea_d', '雅座·瑞雪', '瑞雪桌·3号'),
    ('tea_d_4', 'tea_d', '雅座·瑞雪', '瑞雪桌·4号'),
    # ── 以下两桌需茶舍 Lv3 解锁 ──
    ('tea_e_1', 'tea_e', '雅座·朝露', '朝露桌·1号'),
    ('tea_e_2', 'tea_e', '雅座·朝露', '朝露桌·2号'),
    ('tea_e_3', 'tea_e', '雅座·朝露', '朝露桌·3号'),
    ('tea_e_4', 'tea_e', '雅座·朝露', '朝露桌·4号'),
    ('tea_f_1', 'tea_f', '雅座·晚霞', '晚霞桌·1号'),
    ('tea_f_2', 'tea_f', '雅座·晚霞', '晚霞桌·2号'),
    ('tea_f_3', 'tea_f', '雅座·晚霞', '晚霞桌·3号'),
    ('tea_f_4', 'tea_f', '雅座·晚霞', '晚霞桌·4号'),
]


def seed_tea_house_seats() -> int:
    """补齐缺失的茶舍座位，删除多余的旧座位，返回本次新增条数。"""
    valid_keys = {s[0] for s in TEA_HOUSE_SEATS}
    existing = AdventurerS1TeaHouseSeat.query.all()
    existing_keys = {s.seat_key for s in existing}

    # 删除不在种子列表里的多余座位（如旧版每桌 8 座时遗留的）
    stale_keys = [s.seat_key for s in existing if s.seat_key not in valid_keys]
    if stale_keys:
        AdventurerS1TeaHouseSeat.query.filter(
            AdventurerS1TeaHouseSeat.seat_key.in_(stale_keys),
        ).delete(synchronize_session=False)
        db_manager.commit()

    to_add = [
        AdventurerS1TeaHouseSeat(
            seat_key=seat_key, table_key=table_key,
            table_label=table_label, label=label, sort_order=i,
        )
        for i, (seat_key, table_key, table_label, label) in enumerate(TEA_HOUSE_SEATS)
        if seat_key not in existing_keys
    ]
    if to_add:
        db_manager.add_all(to_add, flush=False)
        db_manager.commit()
    return len(to_add)


# ── 深渊温泉座位种子 ──────────────────────────────────────────────────────────

# 两个 4 人小池 + 一个 24 人大池 = 32 座，全部 Lv1 默认开放（无 Lv2 锁池）。
# seat_key 为幂等判重标识，不要改已上线的值。
HOT_SPRING_SEATS = (
    [
        (f'hs_pool_a_{i}', 'pool_a', '清泉小池', f'清泉池·{i}号')
        for i in range(1, 5)
    ]
    + [
        (f'hs_pool_b_{i}', 'pool_b', '温玉小池', f'温玉池·{i}号')
        for i in range(1, 5)
    ]
    + [
        (f'hs_pool_big_{i}', 'pool_big', '云雾大池', f'云雾大池·{i}号')
        for i in range(1, 25)
    ]
)


def seed_hot_spring_seats() -> int:
    """补齐缺失的温泉座位，删除多余的旧座位，返回本次新增条数。"""
    valid_keys = {s[0] for s in HOT_SPRING_SEATS}
    existing = AdventurerS1HotSpringSeat.query.all()
    existing_keys = {s.seat_key for s in existing}

    stale_keys = [s.seat_key for s in existing if s.seat_key not in valid_keys]
    if stale_keys:
        AdventurerS1HotSpringSeat.query.filter(
            AdventurerS1HotSpringSeat.seat_key.in_(stale_keys),
        ).delete(synchronize_session=False)
        db_manager.commit()

    to_add = [
        AdventurerS1HotSpringSeat(
            seat_key=seat_key, table_key=table_key,
            table_label=table_label, label=label, sort_order=i,
        )
        for i, (seat_key, table_key, table_label, label) in enumerate(HOT_SPRING_SEATS)
        if seat_key not in existing_keys
    ]
    if to_add:
        db_manager.add_all(to_add, flush=False)
        db_manager.commit()
    return len(to_add)


# ── 建筑模板种子 ──────────────────────────────────────────────────────────────

PIGSTY_PIGLET_ITEM_NAME = '泥点小猪'
PIGSTY_PIGLET_ITEM_DESCRIPTION = (
    '一只从酒馆后院猪圈里跟出来的粉色小猪，耳尖和鼻头总沾着几粒泥。'
    '它没有特殊能力，胆子也不大，但嗅觉灵敏、性情亲人，会循着熟悉的脚步一路小跑。'
)

# 旧版 name → code 映射（用于迁移没有 code 的存量模板）
_LEGACY_NAME_TO_CODE = {
    '草药店': 'herbalist_shop',
    '军需店': 'armory_shop',
    '任务栏': 'task_board',
    '宿舍楼·单人间': 'dormitory_single',
    '宿舍楼·双人间': 'dormitory_double',
    '宿舍楼·四人间': 'dormitory_quad',
    '力量训练场': 'training_strength',
    '敏捷训练场': 'training_agility',
    '智力修炼室': 'training_intelligence',
    '铁匠铺': 'blacksmith',
    '熔炉杂货铺': 'forge_supply_shop',
    '治疗所': 'healing_house',
    '医院': 'hospital',
    '疗养所': 'sanatorium',
    '小圣堂': 'chapel',
    '伐木林': 'lumber_mill',
    '采石场': 'quarry',
    '按摩店': 'massage_parlor',
    '洗脚店': 'foot_spa',
    '听曲阁': 'music_pavilion',
    '深渊食堂': 'dining_hall',
    '搬运工': 'porter',
    '酒馆服务生': 'tavern_waiter',
    '巡逻守卫': 'patrol_guard',
    '账房抄写': 'scribe',
    '信使快递': 'courier',
    '赌场荷官': 'casino_dealer',
    '夜间睡岗': 'night_watch_sleep',
    '后院猪圈': 'pigsty_cleaning',
    '个人交易市场': 'personal_market',
    '隐秘拍卖行': 'secret_auction',
    '收购商行': 'buyback_shop',
    '深渊赌坊': 'abyss_gambling_hall',
    '技能书阁': 'skill_school',
    '百艺阁': 'skill_school',
    '幸运大转盘': 'lucky_wheel',
    '英灵碑林': 'fallen_memorial',
    '嬗变祭坛': 'pet_evolution_altar',
    '炼金工房': 'alchemy_workshop',
    '余烬灶坊': 'ember_kitchen',
}

# 随酒馆世界一同存在、无需玩家建造的公共建筑
_PREBUILT_BUILDING_CODES = frozenset({
    'tavern_hall',
    'campaign_hall',
    'transcendent_assessment_court',
})

# 无材料需求、跳过捐献直接进入建造阶段的建筑
_DIRECT_BUILD_CODES = frozenset({'lumber_mill', 'quarry', 'task_board'})
# 兼容旧引用
_GATHERING_DIRECT_BUILD_CODES = _DIRECT_BUILD_CODES

# 需酒馆升至 Lv2 后才解锁建造的建筑：seed 在 Lv1 不创建实例，解锁时由 admin_unlock 补建。
# iron_mine 走 direct build（解锁即建好），smeltery / clinic 走捐材料建造。
_TAVERN_LV2_LOCKED_CODES = frozenset({
    'iron_mine', 'smeltery', 'clinic', 'charcoal_kiln', 'scavenge', 'clue_bureau',
    'alchemy_workshop', 'hot_spring',
})


def make_initial_building_instance(template: AdventurerS1BuildingTemplate) -> AdventurerS1BuildingInstance:
    """按模板规则创建初始建筑实例（seed / 管理员新建唯一建筑共用）。"""
    now = _portable_utc_now()
    if template.code in _PREBUILT_BUILDING_CODES:
        return AdventurerS1BuildingInstance(template_id=template.id, status='built')
    if template.code in _DIRECT_BUILD_CODES:
        hours = template.build_hours or 24
        return AdventurerS1BuildingInstance(
            template_id=template.id,
            status='building',
            build_started_at=now,
            build_complete_at=now + timedelta(hours=hours),
            accumulated_progress_seconds=0,
        )
    inst = AdventurerS1BuildingInstance(template_id=template.id, status='locked')
    if (
        template.required_materials
        and template.code not in _BUILDING_ADMIN_GATED_DONATION_CODES
    ):
        inst.build_started_at = now
    return inst


BUILDING_TEMPLATES = [
    # (code, 名称, 图标, 描述, 类型, 分类, 建造小时数, 帮忙位, 每次精力消耗, 每次加速分钟, 排序
    #  [, handler_key [, 结算配置 [, produced_beds, required_materials]]])
    # handler_key=None 时走 settlement_config / category 默认值
    ('blacksmith', '铁匠铺', '⚒️', '锻造与强化武器防具，由铁匠大师根据材料品质决定强化结果。', 'unique', 'equipment', 24, 2, 25, 60, 1, 'blacksmith', {
        'gold_cost': 200, 'usage_stamina_cost': 25, 'usage_duration_minutes': 1, 'cooldown_hours': 8,
        'description': '锻造完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('tailor_workshop', '绮罗裁坊', '🪡', '不借炉火锻打，而以裁片、针线、编织、层叠与纹饰等精细衣装工艺，将两件投入改制或制作成防具与饰品。', 'unique', 'equipment', 24, 2, 25, 60, 27, 'tailor_workshop', {
        'gold_cost': 300, 'usage_stamina_cost': 25, 'usage_duration_minutes': 1, 'cooldown_hours': 12,
        'description': '裁制完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('alchemy_workshop', '炼金工房', '⚗️', '将草药、矿物与试剂投入炼金釜，炼制药水、提纯材料或炼金工具；与铁匠铺的装备锻造不同，胡乱投入或性质冲突的材料可能全部损毁。酒馆 Lv2 后解锁建造。', 'unique', 'equipment', 24, 2, 25, 60, 24, 'alchemy_workshop', {
        'gold_cost': 300, 'usage_stamina_cost': 25, 'usage_duration_minutes': 1, 'cooldown_hours': 12,
        'description': '炼金完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('item_assembly_workshop', '构装工坊', '🧩', '将 2~5 件物品按玩家指定的排列关系进行组合构装；直线、十字、里外、横向与环形闭环都会影响最终判定。', 'unique', 'equipment', 24, 2, 25, 60, 25, 'item_assembly_workshop', {
        'gold_cost': 1000, 'usage_stamina_cost': 100, 'usage_duration_minutes': 1, 'cooldown_hours': 24,
        'description': '构装完成',
    }, None, {'wood': 200, 'stone': 200, 'iron_ingot': 50}),
    ('ember_kitchen', '余烬灶坊', '🍲', '炉膛里留着不熄的余烬，掌勺人可借建筑自带的炖锅、煎锅、蒸笼、烤盘或石臼，把 1~5 份材料做成便携餐食；无法确认能安全入口的材料只会留下必须当场丢弃的黑暗料理。', 'unique', 'equipment', 24, 2, 25, 60, 26, 'ember_kitchen', {
        'gold_cost': 200, 'usage_stamina_cost': 30, 'usage_duration_minutes': 30,
        'cooldown_hours': 12, 'usage_slots': 4,
        'description': '灶火熄稳，餐食已经出锅',
    }, None, {'wood': 100, 'stone': 100}),
    ('forge_supply_shop', '熔炉杂货铺', '🧰', '贩售残次元素结晶等高风险锻造辅材与探险杂物；辅材贵而不稳，小概率为装备附上元素之力。', 'unique', 'equipment', 24, 2, 20, 60, 2, 'forge_supply_shop', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '挑选商品购买，物品直接进入冒险者随身，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('armory_shop', '军需店', '🛒', '贩售各类入门武器、防具与冒险补给，让新手冒险者快速备齐行装、踏上旅途。', 'unique', 'equipment', 24, 2, 20, 60, 3, 'armory_shop', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '挑选商品购买，物品直接进入冒险者随身，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('herbalist_shop', '草药店', '🌿', '采集并售卖各类草药、治疗药剂与应急消耗品，从基础的绷带药膏到烈性兴奋剂一应俱全，为冒险者提供探险前的补给；迷雾沼泽获救的老药师埃莉诺偶尔来此配制滤息抗瘴类特供药剂。', 'unique', 'medical', 24, 2, 20, 60, 4, 'herbalist_shop', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '挑选商品购买，物品直接进入冒险者随身，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('buyback_shop', '收购商行', '💵', '按类型与稀有度底价折价收购冒险者随身或仓库中的物品，变现为金币。', 'unique', 'equipment', 24, 2, 20, 60, 22, 'buyback_shop', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '出售物品换取金币，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('healing_house', '治疗所', '🏥', '专业的伤势治疗设施，由经验丰富的医师评估并治愈冒险者的肉体伤害（负面特质）。', 'unique', 'medical', 24, 2, 20, 60, 5, 'healing_house', {
        'gold_cost': 100, 'usage_stamina_cost': 20, 'usage_duration_minutes': 1, 'cooldown_hours': 12,
        'description': '治疗完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('hospital', '医院', '⚕️', '付费快速恢复生命值，设有 4 张病床可同时收治伤患。灵体状态的一只丸丸崽还会拿着流星锤在病人旁边帮忙治疗。', 'unique', 'medical', 24, 4, 20, 60, 45, 'hospital', {
        'gold_cost_per_hp': 2,
        'usage_duration_minutes_per_hp': 2,
        'usage_stamina_cost': 0,
        'description': '治疗完成，生命值已恢复',
    }, None, {'wood': 200, 'stone': 200}),
    ('clinic', '深渊手术室', '🔪', '酒馆深处新辟的高级外科手术间，以重金换取时间，让濒危的伤者以最快速度重返深渊，设有 4 张监护床位同时收治。酒馆 Lv2 后解锁建造。', 'unique', 'medical', 24, 2, 20, 60, 48, 'clinic', {
        'gold_cost_per_minute': 10,
        'hp_percent_per_minute': 1,
        'usage_slots': 4,
        'usage_stamina_cost': 0,
        'description': '治疗完成，生命值已恢复',
    }, None, {'wood': 200, 'stone': 200, 'iron_ingot': 50}),
    ('sanatorium', '疗养所', '🌿', '加速负面特质自然恢复的疗养设施，设有 4 张床位；升至 Lv2 床位翻倍（8 张）。', 'unique', 'medical', 24, 2, 20, 60, 46, 'sanatorium', {
        'gold_cost_per_hour': 10,
        'usage_stamina_cost': 0,
        'usage_duration_minutes': 60,
        'usage_slots': 4,
        'description': '疗养完成，恢复期加速已计入',
    }, None, {'wood': 150, 'stone': 150}),
    ('chapel', '小圣堂', '⛪', '冒险者们从废弃修道院的崩塌穹顶下救回的见习修女，如今在酒馆旁收拾出一间祈祷室栖身。她随身的圣徽遗物仍散发着微弱光芒，能对轻度诅咒与邪气做出回应，但深层净化远超她目前的修行——每次祈祷都是一次试探，成功与否很大程度上取决于诅咒本身的强弱。梭哈用生命换回了她，她能做的只有替后来者诵一段经。', 'unique', 'medical', 24, 2, 20, 60, 47, 'chapel', {
        'gold_cost': 100, 'usage_stamina_cost': 20, 'usage_duration_minutes': 1, 'cooldown_hours': 12,
        'description': '祈祷完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('lumber_mill', '伐木林', '🪵', '砍伐木材的林间作业场。', 'unique', 'gathering', 24, 4, 20, 60, 5, None, {
        'reward_type': 'material', 'reward_material_name': 'wood', 'reward_amount': 10,
        'usage_stamina_cost': 30, 'usage_duration_minutes': 30,
        'description': '伐木所得：木材 +10',
    }),
    ('quarry', '采石场', '⛏️', '开采石料的露天矿区。', 'unique', 'gathering', 24, 4, 20, 60, 6, None, {
        'reward_type': 'material', 'reward_material_name': 'stone', 'reward_amount': 10,
        'usage_stamina_cost': 30, 'usage_duration_minutes': 30,
        'description': '采石所得：石料 +10',
    }),
    ('iron_mine', '铁矿山', '⛏️', '开采铁矿石的深层矿脉，设有 8 个采矿工位可同时作业，酒馆 Lv2 后解锁。', 'unique', 'gathering', 24, 2, 20, 60, 6, None, {
        'reward_type': 'material', 'reward_material_name': 'iron_ore', 'reward_amount': 10,
        'usage_stamina_cost': 30, 'usage_duration_minutes': 30,
        'usage_slots': 8,
        'description': '采矿所得：铁矿石 +10',
    }, None, {'wood': 200, 'stone': 200}),
    ('charcoal_kiln', '炭窑', '🏭', '将木材闷烧成耐烧木炭的窑炉，木炭是熔炼铁锭不可或缺的燃料，设有 8 个烧炭工位可同时作业，酒馆 Lv2 后解锁。', 'unique', 'gathering', 24, 2, 20, 60, 6, None, {
        'reward_type': 'material', 'reward_material_name': 'charcoal', 'reward_amount': 10,
        'input_materials': {'wood': 10},
        'usage_stamina_cost': 25, 'usage_duration_minutes': 30,
        'usage_slots': 8,
        'description': '烧炭所得：木炭 +10（消耗木材 10）',
    }, None, {'wood': 200, 'stone': 200}),
    ('smeltery', '熔铁坊', '🔥', '将铁矿石与木炭一同熔炼成铁锭的冶炼设施，设有 8 个冶炼工位可同时作业，Lv2 建造的基础材料来源。', 'unique', 'gathering', 24, 2, 20, 60, 6, None, {
        'reward_type': 'material', 'reward_material_name': 'iron_ingot', 'reward_amount': 1,
        'input_materials': {'iron_ore': 10, 'charcoal': 3},
        'usage_stamina_cost': 10, 'usage_duration_minutes': 30,
        'usage_slots': 8,
        'description': '冶炼所得：铁锭 +1（消耗铁矿石 10、木炭 3）',
    }, None, {'wood': 200, 'stone': 200}),
    ('training_strength', '力量训练场', '💪', '举铁、攀岩、对抗沙袋，用汗水换取更强的力量。', 'unique', 'training', 24, 2, 20, 60, 7, 'training', {
        'reward_type': 'stat', 'reward_amount': 1, 'stat_name': 'strength',
        'usage_stamina_cost': 60, 'usage_duration_minutes': 60, 'daily_limit': 1,
        'description': '锻炼完成，力量 +1',
    }, None, {'wood': 100, 'stone': 100}),
    ('training_agility', '敏捷训练场', '🏃', '闪避训练、绳索穿越、反应测试，让身手更加灵活。', 'unique', 'training', 24, 2, 20, 60, 8, 'training', {
        'reward_type': 'stat', 'reward_amount': 1, 'stat_name': 'agility',
        'usage_stamina_cost': 60, 'usage_duration_minutes': 60, 'daily_limit': 1,
        'description': '训练完成，敏捷 +1',
    }, None, {'wood': 100, 'stone': 100}),
    ('training_intelligence', '智力修炼室', '📖', '冥想、阅读典籍、解谜训练，提升心智与判断力。', 'unique', 'training', 24, 2, 20, 60, 9, 'training', {
        'reward_type': 'stat', 'reward_amount': 1, 'stat_name': 'intelligence',
        'usage_stamina_cost': 60, 'usage_duration_minutes': 60, 'daily_limit': 1,
        'description': '修炼完成，智力 +1',
    }, None, {'wood': 100, 'stone': 100}),
    ('scavenge', '深渊拾荒队', '🔦', '组织冒险者向酒馆外的废墟与险境搜寻遗物：按出门时长消耗金币与精力，归来时从各稀有度物品中带回一批战利品，甚至小概率可获得特殊机缘副本的门票；设有 8 个出发位可同时出门。酒馆 Lv2 后解锁建造。', 'unique', 'gathering', 24, 2, 20, 60, 50, 'scavenge', {
        'gold_cost_per_hour': 20,
        'usage_stamina_cost': 40,
        'usage_slots': 8,
        'max_search_hours': 8,
        'result_source': 'backend',
        'description': '拾荒归来，战利品已收入囊中',
    }, None, {'wood': 200, 'stone': 200}),
    ('clue_bureau', '线索追查处', '🔍', '出门追查隐藏 Boss 线索：按出门时长消耗精力，有机会随机获得一条（入随身装备）。四条线索可由小队成员分别携带，凑齐后前往对应副本出发即可触发隐藏 Boss。设有 8 个出发位可同时出门。酒馆 Lv2 后解锁建造。', 'unique', 'gathering', 24, 2, 20, 60, 52, 'clue_bureau', {
        'stamina_cost_per_hour': 20,
        'clue_drop_prob_per_hour': 0.20,
        'usage_stamina_cost': 0,
        'usage_slots': 8,
        'max_search_hours': 6,
        'description': '线索追查归来',
    }, None, {'wood': 150, 'stone': 150}),
    ('skill_school', '百艺阁', '📜', '旧卷与练习器具堆满长桌，驻阁导师既传旧日招式，也会替冒险者琢磨一门合身的新本事。', 'unique', 'training', 24, 2, 20, 60, 47, 'skill_school', {
        'reward_type': 'none', 'reward_amount': 0,
        'usage_duration_minutes': 60, 'usage_stamina_cost': 0, 'usage_slots': 4,
        'description': '跟随导师练完一课，已记下一门本事',
    }, None, {'wood': 100, 'stone': 100}),
    ('massage_parlor', '按摩店', '💆', '舒缓筋骨、消除疲劳的按摩放松场所。', 'unique', 'leisure', 24, 2, 20, 60, 10, 'massage_parlor', {
        'reward_type': 'stamina', 'reward_amount': 80,
        'gold_cost': 50, 'usage_duration_minutes': 15, 'cooldown_hours': 12,
        'description': '按摩放松，精力 +80',
    }, None, {'wood': 100, 'stone': 100}),
    ('foot_spa', '洗脚店', '🦶', '热水泡脚、驱寒解乏的休憩小店。', 'unique', 'leisure', 24, 2, 20, 60, 11, 'foot_spa', {
        'reward_type': 'stamina', 'reward_amount': 55,
        'gold_cost': 30, 'usage_duration_minutes': 10, 'cooldown_hours': 12,
        'description': '泡脚解乏，精力 +55',
    }, None, {'wood': 100, 'stone': 100}),
    ('music_pavilion', '听曲阁', '🎵', '丝竹悦耳、品茗听曲的雅致消遣之地。', 'unique', 'leisure', 24, 2, 20, 60, 12, 'music_pavilion', {
        'reward_type': 'stamina', 'reward_amount': 65,
        'gold_cost': 40, 'usage_duration_minutes': 12, 'cooldown_hours': 12,
        'description': '品茗听曲，精力 +65',
    }, None, {'wood': 100, 'stone': 100}),
    ('dining_hall', '深渊食堂', '🍲', '花金币点一份热饭，下一趟探险获得小幅临时加成。设有 2 个用餐位，可同时服务两名冒险者。', 'unique', 'leisure', 24, 2, 20, 60, 46, 'dining_hall', {
        'usage_duration_minutes': 10,
        'cooldown_hours': 4,
        'usage_stamina_cost': 0,
        'description': '用餐完成，获得下次探险加成',
    }, None, {'wood': 100, 'stone': 100}),
    ('insurance_shop', '保险局', '🛡️', '冒险者工会旁的小柜台，五种保单均可同时购买且只覆盖并消耗于下一次副本；三种收益兜底险在特殊机缘副本不赔付。', 'unique', 'leisure', 24, 2, 20, 60, 48, 'insurance_shop', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '购买保险，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('hidden_dungeon_exchange', '秘境收藏馆', '🏛️', '陈列各处隐藏副本带回的珍奇素材；冒险者可按固定组合交付藏品，兑换门票、技能卷轴等珍贵物品。藏品仍是普通物品，也可自由交易、锻造或留作他用。', 'unique', 'leisure', 24, 2, 20, 60, 49, 'hidden_dungeon_exchange', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '直接选择配方兑换，无需派遣占位',
    }, None, {'wood': 200, 'stone': 200, 'iron_ingot': 50}),
    ('abyss_gambling_hall', '深渊赌坊', '🎲', '设有 8 个赌桌座位，须入座后方可下注；每分钟一轮集体开注，须有人正在赌场荷官岗位打工时方才开市。', 'unique', 'leisure', 24, 2, 20, 60, 49, 'gambling_hall', {
        'reward_type': 'none', 'reward_amount': 0,
        'usage_slots': 8,
        'usage_duration_minutes': 0,
        'usage_stamina_cost': 0,
        'description': '入座参与每分钟一轮集体猜大小',
        'linked_work_building_code': 'casino_dealer',
    }, None, {'wood': 100, 'stone': 100}),
    ('lucky_wheel', '幸运大转盘', '🎡', '使用大转盘抽奖券抽取奖池物品；可先预览各稀有度奖池内容。', 'unique', 'leisure', 24, 2, 20, 60, 50, 'lucky_wheel', {
        'reward_type': 'none', 'reward_amount': 0,
        'description': '抽奖获得随机物品，无需派遣占位',
    }, None, {'wood': 100, 'stone': 100}),
    ('fallen_memorial', '英灵碑林', '🪦', '阵亡冒险者的归宿。每块墓碑刻着其死亡前的最后叙事，供后人瞻仰与留言。', 'unique', 'memorial', 24, 2, 20, 60, 51, 'fallen_memorial', {
        'reward_type': 'none',
        'reward_amount': 0,
        'description': '瞻仰阵亡同伴的墓碑',
    }, None, {'wood': 100, 'stone': 100}),
    ('flower_shop', '鲜花店', '💐', '为英灵碑林提供不同价位的鲜花。鲜花只能在墓地选定墓碑后送出，仅供纪念展示，24 小时后枯萎。', 'unique', 'memorial', 24, 2, 20, 60, 52, 'flower_shop', {
        'reward_type': 'none',
        'reward_amount': 0,
        'description': '请前往英灵碑林选择墓碑并送花',
    }, None, {'wood': 200, 'stone': 200, 'iron_ingot': 50}),
    (
        'tavern_owner_statue', '酒馆老板的雕像', '🗿',
        '酒馆众人合力为老板立起的巨型雕像。'
        '工程本身不带来任何奖励，只供大家长期捐料、出力并留下共建贡献。',
        'unique', 'memorial', 8760, 12, 20, 60, 53, 'contribution_monument', {
            'reward_type': 'none',
            'reward_amount': 0,
            'description': '雕像仅供瞻仰，不提供任何使用奖励',
        }, None, {'wood': 10000, 'stone': 50000, 'iron_ingot': 10000},
    ),
    (
        'transcendent_assessment_court', '无冕终庭', '✦',
        '黑石终庭无钟无座，只为常阶之外的功业点燃庭火。这里铸下的不是佩在衣襟上的虚衔，'
        '而是将传遍酒馆、城邦与敌阵，令素未谋面之人也能认出你的世界铭号。',
        'unique', 'memorial', 0, 0, 0, 0, 1, 'transcendent_court_preview', {
            'reward_type': 'none',
            'reward_amount': 0,
            'description': '诸席静观，只录已经完成的功业',
        }, None, None,
    ),

    # 打工收益设计基准（2026-06-10 调降）：金币/精力比压到 0.2~0.45，明显低于探险
    # （探险 80 精力产 20~80 金币，比率 0.25~1.0），让打工只是精力过剩时的保底收入。
    # 七个短时岗位每日上限合计 162 金币/人（原六岗位 138），精力约束下实际远低于上限。
    # 夜间睡岗是给玩家离线挂机的低收益长工：8 小时占位、16 小时 CD、0 精力、仅 32 金币。
    ('porter', '搬运工', '📦', '搬运货物、装卸物资的体力活，力气越大工钱越高。', 'multiple', 'work', 12, 2, 20, 60, 13, None, {
        'reward_type': 'gold', 'reward_amount': 10,
        'usage_stamina_cost': 40, 'usage_duration_minutes': 30, 'daily_limit': 2,
        'required_stat': 'strength', 'required_stat_min': 10,
        'description': '搬运完成，获得工钱',
    }, None, {'wood': 100, 'stone': 100}),
    ('tavern_waiter', '酒馆服务生', '🍺', '端酒送菜、招呼客人，手脚麻利才能拿到小费。', 'multiple', 'work', 12, 2, 20, 60, 14, None, {
        'reward_type': 'gold', 'reward_amount': 7,
        'usage_stamina_cost': 25, 'usage_duration_minutes': 20, 'daily_limit': 3,
        'required_stat': 'agility', 'required_stat_min': 10,
        'description': '服务完成，获得工钱和小费',
    }, None, {'wood': 100, 'stone': 100}),
    ('patrol_guard', '巡逻守卫', '🛡️', '在街区巡逻维持治安，需要一定力量威慑宵小。', 'multiple', 'work', 12, 2, 20, 60, 15, None, {
        'reward_type': 'gold', 'reward_amount': 12,
        'usage_stamina_cost': 50, 'usage_duration_minutes': 45, 'daily_limit': 2,
        'required_stat': 'strength', 'required_stat_min': 15,
        'description': '巡逻完成，获得守卫报酬',
    }, None, {'wood': 100, 'stone': 100}),
    ('scribe', '账房抄写', '🧾', '帮商铺记账抄写文书，头脑清晰才能不出差错。', 'multiple', 'work', 12, 2, 20, 60, 16, None, {
        'reward_type': 'gold', 'reward_amount': 9,
        'usage_stamina_cost': 20, 'usage_duration_minutes': 25, 'daily_limit': 3,
        'required_stat': 'intelligence', 'required_stat_min': 12,
        'description': '抄写完成，获得报酬',
    }, None, {'wood': 100, 'stone': 100}),
    ('courier', '信使快递', '🕊️', '替人传信送包裹，跑得快才能接更多单。', 'multiple', 'work', 12, 2, 20, 60, 17, None, {
        'reward_type': 'gold', 'reward_amount': 6,
        'usage_stamina_cost': 30, 'usage_duration_minutes': 15, 'daily_limit': 4,
        'required_stat': 'agility', 'required_stat_min': 8,
        'description': '送达完成，获得跑腿费',
    }, None, {'wood': 100, 'stone': 100}),
    ('casino_dealer', '赌场荷官', '🎰', '在赌场发牌摇骰，运气好的荷官能让客人更愿意打赏。', 'multiple', 'work', 12, 2, 20, 60, 18, None, {
        'reward_type': 'gold', 'reward_amount': 11,
        'usage_stamina_cost': 35, 'usage_duration_minutes': 40, 'daily_limit': 2,
        'required_stat': 'luck', 'required_stat_min': 12,
        'description': '当值完成，获得工钱和打赏',
    }, None, {'wood': 100, 'stone': 100}),
    ('night_watch_sleep', '夜间睡岗', '🌙', '给冒险者找张靠门的长椅守夜打盹，适合玩家离线睡觉时低收益挂机；设有 8 个睡岗位，升至 Lv2 床位翻倍（16 个）。', 'multiple', 'work', 12, 8, 20, 60, 19, None, {
        'reward_type': 'gold', 'reward_amount': 32,
        'usage_stamina_cost': 0, 'usage_duration_minutes': 480, 'cooldown_hours': 16,
        'usage_slots': 8,
        'rest_like_recovery': True,
        'description': '睡岗结束，获得少量守夜工钱；睡岗期间按初级宿舍速度恢复精力和血量，但不累计特质恢复',
    }, None, {'wood': 100, 'stone': 100}),
    (
        'pigsty_cleaning', '后院猪圈', '🐖',
        '酒馆后院的普通猪圈。派冒险者添满饲槽、补足清水并照看猪群，完成后领取一份低廉工钱；'
        '喂猪时有小概率获得普通品质伙伴「泥点小猪」。',
        'multiple', 'work', 12, 2, 20, 60, 20, 'pigsty_cleaning', {
            'reward_type': 'gold', 'reward_amount': 8,
            'usage_stamina_cost': 35, 'usage_duration_minutes': 30, 'daily_limit': 3,
            'required_stat': 'strength', 'required_stat_min': 8,
            'piglet_reward_chance': 0.01,
            'piglet_reward_item': PIGSTY_PIGLET_ITEM_NAME,
            'description': '喂猪完成，获得工钱；有小概率获得普通品质伙伴「泥点小猪」',
        }, None, {'wood': 100, 'stone': 100},
    ),
    ('personal_market', '个人交易市场', '🏪', '冒险者之间自由交易物品的场所，同稀有度以物易物，成交时双方各付手续费。', 'unique', 'market', 24, 2, 20, 60, 19, None, None, None, {'wood': 200, 'stone': 200}),
    ('pet_evolution_altar', '嬗变祭坛', '🧬', '古老的嬗变圣坛，守护者沉眠于此，能引导同伴经历嬗变、突破为更高阶的存在。', 'unique', 'equipment', 24, 2, 20, 60, 23, 'pet_evolution', {
        'gold_cost': 200, 'usage_stamina_cost': 25, 'usage_duration_minutes': 1, 'cooldown_hours': 24,
        'description': '嬗变完成',
    }, None, {'wood': 100, 'stone': 100}),
    ('secret_auction', '隐秘拍卖行', '🎭', '拍卖随机稀有商品，玩家密封出价仅可见人数；到期最高者得，同价则流拍。', 'unique', 'auction', 24, 2, 20, 60, 21, 'secret_auction', {
        'reward_type': 'none',
        'reward_amount': 0,
        'description': '密封出价参与拍卖，无需派遣冒险者占位',
    }, None, {'wood': 200, 'stone': 200}),
    ('task_board', '任务栏', '📜', '张贴各类冒险任务的公告栏，冒险者可在此接取任务获取报酬。', 'unique', 'task', 4, 2, 20, 60, 20, 'task_board', None, None, None),
    ('campaign_hall', '战役议事厅', '🗺️', '汇集各地发现与小队战报，见证世界变化，并召集冒险者参与接下来的行动。', 'unique', 'task', 0, 0, 0, 0, 22, 'campaign_hall', None),
    # 酒馆大堂：handler 占位 + 状态标签，非可进入建筑；UI 只在「酒馆」tab 展示，不进建筑分区
    ('tavern_hall', '酒馆大堂', '🍺', '冒险者聚集饮酒的地方，入座后可点酒休息，缓慢恢复精力。', 'unique', 'general', 0, 0, 0, 0, 0, 'tavern_hall', None),
    # 深渊茶舍：座位系统独立管理，入座扣 30 金币，精力 2/分钟恢复
    ('tea_house', '深渊茶舍', '🍵', '安静的喝茶场所，开放清风、明月两桌共八座，入座后精力恢复速率提升至每分钟 2 点；Lv2 再添落花、瑞雪两桌，Lv3 再添朝露、晚霞两桌。', 'unique', 'leisure', 24, 2, 20, 60, 21, 'tea_house', None, None, {'wood': 100, 'stone': 100}),
    # 深渊温泉：座位系统独立管理，酒馆 Lv2 后解锁建造；入座扣 200 金币，生命 1 点/2 分钟、精力 1 点/1 分钟，泡满或超 6 小时自动离场
    ('hot_spring', '深渊温泉', '♨️', '热气蒸腾的露天温泉，设清泉、温玉两个小池（各四座）与一个二十四座的云雾大池。入座后生命值每两分钟、精力每一分钟恢复一点，泡满或最多六小时后自动起身，适合伤后慢慢将养。酒馆 Lv2 后解锁建造。', 'unique', 'leisure', 24, 2, 20, 60, 25, 'hot_spring', None, None, {'wood': 150, 'stone': 150}),
    # ── 宿舍楼：玩家自建，按床位数线性扣材料 + 占建造时间 ──
    # 每床位 6 build_hours + 20 木 + 15 石；helper_slots=2、stamina/speedup 沿用其他建筑
    ('dormitory_single', '宿舍楼·单人间', '🛏️', '一间私人小屋，专属一名冒险者。', 'multiple', 'dormitory', 6, 2, 20, 60, 100, 'dormitory', None, 1, {'wood': 20, 'stone': 15}),
    ('dormitory_double', '宿舍楼·双人间', '🛏️', '两张床并排的房间，住进两名冒险者，相处融洽时还能加深羁绊。', 'multiple', 'dormitory', 12, 2, 20, 60, 101, 'dormitory', None, 2, {'wood': 40, 'stone': 30}),
    ('dormitory_quad', '宿舍楼·四人间', '🛏️', '宽敞的集体宿舍，热闹喧哗，新人冒险者最常落脚的地方。', 'multiple', 'dormitory', 24, 2, 20, 60, 102, 'dormitory', None, 4, {'wood': 80, 'stone': 60}),
]


# ── 建筑升级配置（Lv2） ─────────────────────────────────────────────────────
# 占位数值：材料消耗后续换新材料，当前用 wood/stone 占位。
# 通用效果：所有可升级建筑 Lv2 +2 使用位（usage_slots_bonus）。
# 特殊效果：handler_effects 由各 handler 自定义读取。
BUILDING_UPGRADE_CONFIGS: dict[str, dict] = {
    # 只有用户明确指定效果的建筑才开放升级，其余 upgrade_config=None 不可升级。
    # Lv2 升级需要铁锭（熔铁坊产出），采石场 Lv2 额外产出铁矿石。
    # ── 装备与制作区 ──
    'ember_kitchen': {
        'cost': {'wood': 100, 'stone': 100, 'iron_ingot': 5},
        'upgrade_hours': 6,
        'usage_slots_bonus': 4,
        'description': 'Lv2：灶位翻倍（4→8）',
        'handler_effects': {},
    },
    # ── 医疗区 ──
    'healing_house': {
        'cost': {'wood': 60, 'stone': 50, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：治疗位+2，可使用治疗物品辅助治疗',
        'handler_effects': {'item_treatment': True},
    },
    'hospital': {
        'cost': {'wood': 100, 'stone': 100, 'iron_ingot': 5},
        'upgrade_hours': 6,
        'usage_slots_bonus': 4,
        'description': 'Lv2：病床翻倍（4→8）',
        'handler_effects': {},
    },
    'sanatorium': {
        'cost': {'wood': 150, 'stone': 150, 'iron_ingot': 5},
        'upgrade_hours': 6,
        'usage_slots_bonus': 4,
        'description': 'Lv2：床位翻倍（4→8）',
        'handler_effects': {},
    },
    'night_watch_sleep': {
        'cost': {'wood': 100, 'stone': 100, 'iron_ingot': 5},
        'upgrade_hours': 6,
        'usage_slots_bonus': 8,
        'description': 'Lv2：睡岗位翻倍（8→16）',
        'handler_effects': {},
    },
    # ── 训练区 ──
    'training_strength': {
        'cost': {'wood': 60, 'stone': 60, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：训练位翻倍（2→4）',
        'handler_effects': {},
    },
    'training_agility': {
        'cost': {'wood': 60, 'stone': 60, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：训练位翻倍（2→4）',
        'handler_effects': {},
    },
    'training_intelligence': {
        'cost': {'wood': 60, 'stone': 60, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：训练位翻倍（2→4）',
        'handler_effects': {},
    },
    # ── 采集区 ──
    'lumber_mill': {
        'cost': {'wood': 40, 'stone': 30, 'iron_ingot': 2},
        'upgrade_hours': 3,
        'usage_slots_bonus': 4,
        'description': 'Lv2：作业位+4',
        'handler_effects': {},
    },
    'quarry': {
        'cost': {'wood': 30, 'stone': 40, 'iron_ingot': 2},
        'upgrade_hours': 3,
        'usage_slots_bonus': 4,
        'description': 'Lv2：作业位+4',
        'handler_effects': {},
    },
    # ── 休闲区 ──
    'massage_parlor': {
        'cost': {'wood': 50, 'stone': 40, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：服务位+2，恢复精力+5',
        'handler_effects': {'stamina_restore_bonus': 5},
    },
    'foot_spa': {
        'cost': {'wood': 50, 'stone': 40, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：服务位+2，恢复精力+5',
        'handler_effects': {'stamina_restore_bonus': 5},
    },
    'music_pavilion': {
        'cost': {'wood': 50, 'stone': 40, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：服务位+2，恢复精力+5',
        'handler_effects': {'stamina_restore_bonus': 5},
    },
    'dining_hall': {
        'cost': {'wood': 60, 'stone': 50, 'iron_ingot': 3},
        'upgrade_hours': 4,
        'usage_slots_bonus': 2,
        'description': 'Lv2：用餐位+2，所有菜品额外恢复10精力',
        'handler_effects': {'stamina_restore': 10},
    },
    'tea_house': {
        # 茶舍走独立座位系统，不用 usage_slots；根级配置保持兼容 Lv2，levels 追加更高等级。
        'cost': {'wood': 200, 'stone': 200, 'iron_ingot': 50},
        'upgrade_hours': 24,
        'usage_slots_bonus': 0,
        'description': 'Lv2：新增落花、瑞雪两桌，茶座 8 → 16',
        'handler_effects': {},
        'max_level': 3,
        'levels': {
            '3': {
                'cost': {'stone': 400, 'wood': 400, 'iron_ingot': 100},
                'upgrade_hours': 24,
                'description': 'Lv3：新增朝露、晚霞两桌，茶座 16 → 24',
            },
        },
    },
    # ── 宿舍楼（每个实例独立升级）──
    'dormitory_single': {
        'cost': {'wood': 20, 'stone': 15, 'iron_ingot': 2},
        'upgrade_hours': 3,
        'usage_slots_bonus': 0,
        'description': 'Lv2：房间变好看了，精力恢复速度+10%',
        'handler_effects': {'recovery_rate_bonus_pct': 10},
    },
    'dormitory_double': {
        'cost': {'wood': 40, 'stone': 30},
        'upgrade_hours': 6,
        'usage_slots_bonus': 0,
        'description': 'Lv2：房间变好看了，精力恢复速度+10%',
        'handler_effects': {'recovery_rate_bonus_pct': 10},
    },
    'dormitory_quad': {
        'cost': {'wood': 80, 'stone': 60},
        'upgrade_hours': 12,
        'usage_slots_bonus': 0,
        'description': 'Lv2：房间变好看了，精力恢复速度+10%',
        'handler_effects': {'recovery_rate_bonus_pct': 10},
    },
}

# ── Lv2 升级成本统一规则（2026-06-24，覆盖上方各 entry 的 cost / upgrade_hours）──
# 升级时长统一 24h；除宿舍外材料统一 wood200+stone200+iron_ingot50；
# 宿舍按床位等比（单人 1× / 双人 2× / 四人 4×）。改成本只需改这里。
_DORM_UPGRADE_COSTS = {
    'dormitory_single': {'wood': 50, 'stone': 50, 'iron_ingot': 10},
    'dormitory_double': {'wood': 100, 'stone': 100, 'iron_ingot': 20},
    'dormitory_quad': {'wood': 200, 'stone': 200, 'iron_ingot': 40},
}
for _u_code, _u_cfg in BUILDING_UPGRADE_CONFIGS.items():
    _u_cfg['upgrade_hours'] = 24
    _u_cfg['cost'] = dict(_DORM_UPGRADE_COSTS.get(
        _u_code, {'wood': 200, 'stone': 200, 'iron_ingot': 50},
    ))
# 以下建筑 upgrade_config=None，暂不开放升级：
# 装备区：铁匠铺、熔炉杂货铺、军需店、收购商行
# 医疗区：小圣堂、草药店
# 训练区：百艺阁
# 休闲区：深渊赌坊、幸运大转盘
# 打工区：搬运工、酒馆服务生、巡逻守卫、账房抄写、信使快递、赌场荷官
# 其他：酒馆大堂、英灵碑林、无冕终庭、个人交易市场、隐秘拍卖行、任务栏


def _migrate_legacy_template_codes() -> int:
    """将存量模板的 code 对齐 _LEGACY_NAME_TO_CODE。

    早期迁移可能把 code 设成中文 name（如「个人交易市场」），导致 seed 条目
    personal_market 对不上、required_materials 无法同步、玩家列表被过滤。
    """
    updated = 0
    for name, expected_code in _LEGACY_NAME_TO_CODE.items():
        for tpl in AdventurerS1BuildingTemplate.query.filter_by(name=name).all():
            if tpl.code != expected_code:
                tpl.code = expected_code
                updated += 1
    if updated:
        db_manager.commit()
    return updated


def seed_building_templates() -> int:
    """补齐缺失的建筑模板，并同步已有模板的可变字段，返回本次新增条数。

    按 code 匹配：code 是稳定标识，改名不影响种子对应。
    存量模板没有 code 的，用 _LEGACY_NAME_TO_CODE 按 name 补上。
    """
    # 先给没有 code 的存量模板补 code（迁移旧数据）
    # code 列默认值为 ''，所以同时兼容 NULL 和空字符串两种历史状态
    from sqlalchemy import or_
    legacy_rows = AdventurerS1BuildingTemplate.query.filter(
        or_(
            AdventurerS1BuildingTemplate.code.is_(None),
            AdventurerS1BuildingTemplate.code == '',
        )
    ).all()
    if legacy_rows:
        for tpl in legacy_rows:
            tpl.code = _LEGACY_NAME_TO_CODE.get(tpl.name) or tpl.name.replace('·', '_').replace(' ', '_')
        db_manager.commit()
    _migrate_legacy_template_codes()

    existing_map = {t.code: t for t in AdventurerS1BuildingTemplate.query.all()}
    to_add = []
    updated = 0
    for entry in BUILDING_TEMPLATES:
        code, name, icon, desc, btype, category, hours, slots, stamina, speedup, sort_order = entry[:11]
        handler_key = entry[11] if len(entry) > 11 else None
        settle_cfg = entry[12] if len(entry) > 12 else None
        produced_beds = entry[13] if len(entry) > 13 else None
        required_materials = entry[14] if len(entry) > 14 else None
        if code == 'transcendent_assessment_court':
            settle_cfg = dict(settle_cfg or {})
            settle_cfg['required_level'] = max(1, int(current_app.config.get(
                'ADVENTURER_S1_TRANSCENDENT_ASSESSMENT_MIN_LEVEL', 20,
            )))

        if code in existing_map:
            t = existing_map[code]
            changed = False
            # 鲜花店曾作为旧功能下线；本次重新纳入正式建筑后需恢复存量模板。
            if code == 'flower_shop' and not t.is_active:
                t.is_active = True; changed = True
            if t.name != name:
                t.name = name; changed = True
            if t.icon != icon:
                t.icon = icon; changed = True
            if t.description != desc:
                t.description = desc; changed = True
            if t.category != category:
                t.category = category; changed = True
            if t.build_hours != hours:
                t.build_hours = hours; changed = True
            if t.helper_slots != slots:
                t.helper_slots = slots; changed = True
            if t.stamina_cost_per_help != stamina:
                t.stamina_cost_per_help = stamina; changed = True
            if t.speedup_minutes != speedup:
                t.speedup_minutes = speedup; changed = True
            if t.sort_order != sort_order:
                t.sort_order = sort_order; changed = True
            if len(entry) > 11 and t.handler_key != handler_key:
                t.handler_key = handler_key; changed = True
            if settle_cfg and t.settlement_config != settle_cfg:
                t.settlement_config = settle_cfg; changed = True
            if produced_beds is not None and t.produced_beds != produced_beds:
                t.produced_beds = produced_beds; changed = True
            if code in _GATHERING_DIRECT_BUILD_CODES:
                if t.required_materials is not None:
                    t.required_materials = None; changed = True
            elif required_materials is not None and t.required_materials != required_materials:
                t.required_materials = required_materials; changed = True
            # 同步升级配置
            upgrade_cfg = BUILDING_UPGRADE_CONFIGS.get(code)
            if t.upgrade_config != upgrade_cfg:
                t.upgrade_config = upgrade_cfg; changed = True
            if changed:
                updated += 1
        else:
            upgrade_cfg = BUILDING_UPGRADE_CONFIGS.get(code)
            to_add.append(AdventurerS1BuildingTemplate(
                code=code, name=name, icon=icon, description=desc,
                building_type=btype, category=category,
                build_hours=hours, helper_slots=slots,
                stamina_cost_per_help=stamina, speedup_minutes=speedup,
                sort_order=sort_order, handler_key=handler_key,
                settlement_config=settle_cfg,
                produced_beds=produced_beds, required_materials=required_materials,
                upgrade_config=upgrade_cfg,
            ))
    if to_add:
        db_manager.add_all(to_add, flush=False)
    if to_add or updated:
        _commit_building_seed_or_raise('模板')
    return len(to_add)


# 已从 BUILDING_TEMPLATES 移除、不再对玩家开放的建筑 code
_RETIRED_BUILDING_CODES = frozenset({'commodity_exchange'})


def deactivate_retired_building_templates() -> int:
    """停用已下线建筑模板（存量库兼容）。"""
    updated = 0
    for code in _RETIRED_BUILDING_CODES:
        tpl = AdventurerS1BuildingTemplate.query.filter_by(code=code).first()
        if tpl and tpl.is_active:
            tpl.is_active = False
            updated += 1
    if updated:
        db_manager.commit()
    return updated


def _find_orphan_lv2_locked_instances() -> list:
    """返回酒馆未升 Lv2 时可清理的 Lv2 锁定建筑无主残留实例（ORM 列表，不删除）。

    判定：`_TAVERN_LV2_LOCKED_CODES` 模板的实例，排除「有冒险者挂载」或「有捐献记录」的
    （那些是玩家有投入的，留给人工）。酒馆已升 Lv2（tavern_level>=2）时这些实例合法，返回 []。
    preview（admin 预览）与 purge（清理）共用此判定，保证预览所见即所删。
    """
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    if int(S1SettingsService.get('tavern_level') or 1) >= 2:
        return []

    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1BuildingDonation

    locked_tpl_ids = [
        t.id for t in AdventurerS1BuildingTemplate.query.filter(
            AdventurerS1BuildingTemplate.code.in_(_TAVERN_LV2_LOCKED_CODES)
        ).all()
    ]
    if not locked_tpl_ids:
        return []
    insts = AdventurerS1BuildingInstance.query.filter(
        AdventurerS1BuildingInstance.template_id.in_(locked_tpl_ids)
    ).all()
    if not insts:
        return []
    inst_ids = [i.id for i in insts]

    # 有冒险者挂载（帮工 / 使用）或有捐献记录的实例：保留，留给人工
    occupied_ids = {
        row[0] for row in AdventurerS1.query
        .filter(AdventurerS1.building_instance_id.in_(inst_ids))
        .with_entities(AdventurerS1.building_instance_id).all()
    }
    donated_ids = {
        row[0] for row in AdventurerS1BuildingDonation.query
        .filter(AdventurerS1BuildingDonation.building_instance_id.in_(inst_ids))
        .with_entities(AdventurerS1BuildingDonation.building_instance_id)
        .distinct().all()
    }
    return [
        inst for inst in insts
        if inst.id not in occupied_ids and inst.id not in donated_ids
    ]


def _purge_orphan_lv2_locked_instances() -> int:
    """删除 Lv2 锁定建筑（酒馆未升 Lv2 时）的无主残留实例，返回删除条数。

    正常 Lv1 这些建筑不应存在实例（seed_building_instances 跳过创建），但 dump 还原、
    历史数据或 GM 误操作可能留下残留，导致管理员视图把它们显示成「建造中 / 募集中」。
    判定见 _find_orphan_lv2_locked_instances（只删无帮工 + 无捐献的纯净残留）。

    seed_all（启动 / 赛季重置）自动调用；但**本地 debug 模式下 app._init_database 会被
    `app.debug and WERKZEUG_RUN_MAIN!='true'` 守卫直接 return，seed_all 不执行**，故另由
    admin 后台按钮手动触发（AdventurerS1Service.admin_purge_orphan_lv2_instances）。
    """
    deletable = _find_orphan_lv2_locked_instances()
    del_ids = [inst.id for inst in deletable]
    if not del_ids:
        return 0
    AdventurerS1BuildingInstance.query.filter(
        AdventurerS1BuildingInstance.id.in_(del_ids)
    ).delete(synchronize_session=False)
    db.session.commit()

    import logging
    logging.getLogger(__name__).info(
        '[S1 seed] 清理 Lv2 锁定建筑无主残留实例: %s', del_ids,
    )
    return len(del_ids)


def seed_building_instances() -> int:
    """为每个建筑模板补齐至少一个实例，返回本次新增条数。multiple 类型可由管理员再追加。

    宿舍楼（category='dormitory'）跳过：宿舍实例只由玩家通过 request_new_dormitory
    申请创建，seed 不应自动建（否则 applicant_user_id 为 None 占并发名额）。
    """
    # 清理历史遗留的孤儿宿舍实例（seed 自动创建、无申请人，占并发名额）
    dorm_tpl_ids = [
        t.id for t in AdventurerS1BuildingTemplate.query.filter_by(category='dormitory').all()
    ]
    if dorm_tpl_ids:
        orphaned = AdventurerS1BuildingInstance.query.filter(
            AdventurerS1BuildingInstance.template_id.in_(dorm_tpl_ids),
            AdventurerS1BuildingInstance.applicant_user_id.is_(None),
            AdventurerS1BuildingInstance.status.in_(['locked', 'building']),
        ).all()
        for inst in orphaned:
            db.session.delete(inst)
        if orphaned:
            db.session.commit()

    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    tavern_level = int(S1SettingsService.get('tavern_level') or 1)
    templates = AdventurerS1BuildingTemplate.query.filter_by(is_active=True).all()
    existing_ids = {i.template_id for i in AdventurerS1BuildingInstance.query.all()}
    to_add = []
    for t in templates:
        if t.category == 'dormitory':
            continue  # 宿舍由玩家申请，不自动 seed
        if t.code in _TAVERN_LV2_LOCKED_CODES and tavern_level < 2:
            continue  # 需酒馆 Lv2 解锁，升级后由 admin_unlock_tavern_upgrade 补建
        if t.id not in existing_ids:
            to_add.append(make_initial_building_instance(t))
    if to_add:
        db_manager.add_all(to_add, flush=False)
        _commit_building_seed_or_raise('实例')
    return len(to_add)


def sync_building_instance_states() -> int:
    """将存量实例对齐初始阶段规则（幂等，启动/seed 时调用）。

    - 有材料需求的 locked 实例：默认开放捐献（build_started_at）；花店等管理员门控建筑除外
    - 采集区伐木林/采石场：跳过捐献，locked → building
    """
    updated = 0
    now = _portable_utc_now()
    instances = AdventurerS1BuildingInstance.query.all()
    for inst in instances:
        tpl = inst.template
        if not tpl or tpl.category == 'dormitory' or tpl.code in ('tavern_hall', 'tea_house', 'hot_spring'):
            continue
        if tpl.code in _DIRECT_BUILD_CODES:
            total_build_seconds = int((tpl.build_hours or 24) * 3600)
            # 旧版本启动同步会把已经建成的采集区/任务栏重新置为 building。
            # 若正式服已经留下这种状态，启动时只恢复建筑状态，不释放挂在上面的采集/任务使用者。
            if (
                inst.status == 'building'
                and (inst.accumulated_progress_seconds or 0) >= total_build_seconds
            ):
                inst.status = 'built'
                inst.accumulated_progress_seconds = total_build_seconds
                if not inst.build_complete_at:
                    inst.build_complete_at = now
                updated += 1
                continue
            if inst.status == 'built':
                continue
            if inst.status != 'building':
                inst.status = 'building'
                updated += 1
            if not inst.build_started_at:
                inst.build_started_at = now
                updated += 1
            hours = tpl.build_hours or 24
            if not inst.build_complete_at:
                inst.build_complete_at = now + timedelta(hours=hours)
                updated += 1
            if inst.accumulated_progress_seconds is None:
                inst.accumulated_progress_seconds = 0
                updated += 1
        elif (
            tpl.required_materials
            and inst.status == 'locked'
            and not inst.build_started_at
            and tpl.code not in _BUILDING_ADMIN_GATED_DONATION_CODES
        ):
            inst.build_started_at = now
            updated += 1
    if updated:
        db_manager.commit()
    return updated


# ── 草药店初始商品 ─────────────────────────────────────────────────────────
# 名称必须包含 AI 探险 prompt 中识别的治疗关键词：
# ('治疗药水', '高级治疗药水', '绷带', '急救', '圣水', '回复', '疗伤')
# 价格参考 S1 消耗品带 5~25；对齐军需店 consumable 档（火把 3 / 箭矢 12 金·10 支），
# 药水略贵于绷带但仍低于入门武器（12~28），100 金起步能备 1~2 瓶再下本。
# 格式：(名称, 类型, 描述, 稀有度, 价格, 满库存)
S1_HERBALIST_ITEMS = [
    ('初级治疗药水', 'consumable',
     '一小瓶淡红色药水，能快速愈合轻伤、止血止痛；对重伤或骨折效果甚微，喝多了还会产生耐药。', 'common', 12, 20),
    ('急救绷带', 'consumable',
     '浸过药液的亚麻绷带，缠在伤口上能持续止血并缓慢愈合；包扎需要时间与技巧，战斗中仓促使用效果打折。', 'common', 5, 30),
    ('消毒药膏', 'consumable',
     '草药与酒精调制的糊状药膏，涂抹在伤口上能清洗创面、防止感染恶化；本身没有愈合作用，但能为后续治疗争取时间。', 'common', 8, 20),
    ('烧伤膏', 'consumable',
     '以芦荟和冰薄荷为基底的清凉药膏，专门缓解火焰灼伤带来的剧痛与水泡；对刀剑创伤或普通外伤无明显效果。', 'common', 10, 15),
    ('止痛草药', 'consumable',
     '嚼服后能迅速压制疼痛感、让伤者恢复行动能力的野生草药；效果持续有限，大量服用会导致嗜睡甚至昏迷。', 'common', 6, 25),
    ('强效兴奋剂', 'consumable',
     '矮人炼金术士秘制的烈性药剂，饮下后短时间内精神亢奋、痛觉迟钝、体能爆发；药效消退后会陷入严重疲惫，频繁使用对心脏负担极大。', 'common', 18, 10),
    ('解毒草', 'consumable',
     '嚼碎后敷于伤口或煎水内服的苦味草药，能中和轻度蛇毒、虫毒与植物毒素；对诅咒类毒素或高阶魔物的烈性毒液几乎无效，服用后会短暂恶心反胃。', 'common', 18, 20),
    ('月见草滤息剂', 'consumable',
     '迷雾沼泽获救的老药师埃莉诺亲制的滤息药剂，以月见草与活性炭调和，能够减轻瘴气与孢子引起的头晕目眩；对刀剑创伤无效，凉口微苦。', 'common', 22, 15),
    ('沼泽抗瘴丸', 'consumable',
     '埃莉诺依照迷雾沼泽样本调制的预防性药丸，出发前含服可稍增对轻度毒素的耐受；无法解诅咒或高阶魔毒，过量服用会犯困。', 'common', 16, 18),
    ('小鱼干', 'consumable',
     '溪鱼剖洗后以薄盐风干的小份旅粮，三尾扎成一束；人可以充饥，酒馆里的猫似乎更懂得欣赏它。', 'common', 6, 40),
]


# 草药店类物品名称白名单（防囤判定专用）。新加品类时在 S1_HERBALIST_ITEMS 里追加即可同步生效。
HERBALIST_ITEM_NAMES = frozenset(item[0] for item in S1_HERBALIST_ITEMS)


def is_herbalist_item(name) -> bool:
    """判定一个物品名是否属于草药店类（受 HERBALIST_CARRY_LIMIT 随身上限约束）。"""
    return bool(name) and name in HERBALIST_ITEM_NAMES


# ── 任务/活动图鉴物品（非商店售卖） ─────────────────────────────────────────
# 格式：(名称, 类型, 描述, 稀有度)
_LEGACY_TREASURE_MAP_ITEM_NAMES = (
    '哥布林矿坑的藏宝图',
    '前任冒险者的私房钱藏宝图',
)
TREASURE_MAP_ITEM_NAME = '前任冒险者的私房钱藏宝图-哥布林矿坑'
# 兼容旧 import
GOBLIN_MINE_TREASURE_MAP_NAME = TREASURE_MAP_ITEM_NAME


def treasure_map_use_condition() -> dict:
    """藏宝图使用条件：仅在与配置一致的副本探险时对 AI 可见。"""
    from flask import current_app, has_app_context
    dungeon = '哥布林矿坑'
    if has_app_context():
        dungeon = str(current_app.config.get(
            'ADVENTURER_S1_TREASURE_MAP_DUNGEON',
            dungeon,
        )).strip() or dungeon
    return {'expedition_dungeon': dungeon}


BIRTHDAY_CAKE_ITEM_NAME = '星灯归愿蛋糕'
BIRTHDAY_CAKE_ITEM_DESCRIPTION = (
    '星灯礼堂只在寿星赴约时烤制的小蛋糕。浅金奶油间藏着蜂蜜、星莓和一圈可分食的糖星，'
    '盒盖内侧写着：「愿你走过新的年岁，仍有归来的门、同坐的人，以及一盏为你留着的灯。」'
    '它只保存今夜的祝愿，不带来任何能力变化。'
)


def birthday_cake_use_condition() -> dict:
    """生日赠礼可正常保管或交易，但名称、描述与改造路径不可改写。"""
    return {'immutable': True}


S1_TASK_CATALOG_ITEMS = [
    (TREASURE_MAP_ITEM_NAME, 'scroll',
     '【仅限哥布林矿坑】按图寻路至矿坑深处可挖到前任冒险者藏匿的大量金币与随机物品。'
     '离开该副本或带入其他地牢均无法生效。使用一次后失效。', 'rare'),
    ('大转盘抽奖券', 'scroll',
     '可在幸运大转盘抽奖一次。', 'common'),
    ('职业通用技能卷轴', 'scroll',
     '进入副本将使用，学习与自己职业相关的一个技能，无职业无效，一次性消耗品，使用完消失。', 'epic'),
    (PIGSTY_PIGLET_ITEM_NAME, 'companion', PIGSTY_PIGLET_ITEM_DESCRIPTION, 'common'),
    (BIRTHDAY_CAKE_ITEM_NAME, 'consumable',
     BIRTHDAY_CAKE_ITEM_DESCRIPTION, 'rare'),
]


# ── 幸运大转盘奖池图鉴（非商店售卖） ─────────────────────────────────────────
# 格式：(名称, 类型, 描述, 稀有度 [, use_condition])
LUCKY_WHEEL_TICKET_ITEM_NAME = '大转盘抽奖券'

LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS = {
    '骑士': '叹息地牢',
    '游侠': '野狼林道',
    '牧师': '废弃修道院',
    '法师': '风暴尖塔',
}

CLASS_TRANSFER_EXCHANGE_DUNGEONS = {
    '战士': '荒野哨站',
    '野蛮人': '食人魔要塞',
    '吟游诗人': '叹息地牢',
    '德鲁伊': '迷雾沼泽',
    '武僧': '古王陵寝',
    '圣武士': '黑日王庭',
    '盗贼': '暗影迷宫',
    '术士': '熔岩火山',
    '黑日誓裁者': '黑日王庭',
    '星渊织法者': '陨落星都',
}

CLASS_TRANSFER_INVITATION_DUNGEONS = {
    **LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS,
    **CLASS_TRANSFER_EXCHANGE_DUNGEONS,
}


def class_transfer_use_condition(target_class: str) -> dict:
    """转职邀请函：仅限对应副本生效，且禁止作为物品变换素材。"""
    dungeon = CLASS_TRANSFER_INVITATION_DUNGEONS.get(target_class, '')
    return {
        'expedition_dungeon': dungeon,
        'target_class': target_class,
        'immutable': True,
    }


def _class_transfer_invitation_desc(target_class: str, dungeon: str) -> str:
    return (
        f'【仅限{dungeon}】正式转职凭证。携带者在该副本完成足够重的转职试炼或仪式后，'
        f'可确立「{target_class}」职业；离开该副本或带入其他地牢均无法生效。使用一次后失效。'
    )


_LEGENDARY_CLASS_TRANSFER_DESCRIPTIONS = {
    '黑日誓裁者': (
        '【仅限黑日王庭】正式转职凭证。携带者须在失去太阳的王庭中公开立下足以约束自身的裁决誓约，'
        '并以行动承担其代价，方可确立「黑日誓裁者」职业；该职业以自我约束换取守护与裁决之力，'
        '不能凭空认定罪责，违背自己的誓约会招致反噬。离开该副本或带入其他地牢均无法生效。使用一次后失效。'
    ),
    '星渊织法者': (
        '【仅限陨落星都】正式转职凭证。携带者须在破碎星轨与错位空间之间建立稳定锚点，'
        '维持一条足以让同伴安全通过的真实路径，方可确立「星渊织法者」职业；该职业擅长固定空间锚点、'
        '偏转行进轨迹并稳定队伍移动，不能逆转时间、任意传送或绕过封闭领域。离开该副本或带入其他地牢均无法生效。'
        '使用一次后失效。'
    ),
}

S1_CLASS_TRANSFER_EXCHANGE_ITEMS = [
    (
        f'{target_class}转职邀请函',
        'scroll',
        _LEGENDARY_CLASS_TRANSFER_DESCRIPTIONS.get(target_class)
        or _class_transfer_invitation_desc(target_class, dungeon),
        'legendary' if target_class in _LEGENDARY_CLASS_TRANSFER_DESCRIPTIONS else 'epic',
        class_transfer_use_condition(target_class),
    )
    for target_class, dungeon in CLASS_TRANSFER_EXCHANGE_DUNGEONS.items()
]


S1_LUCKY_WHEEL_EXCLUSIVE_ITEMS = [
    # 史诗：四职业转职邀请函
    ('骑士转职邀请函', 'scroll',
     _class_transfer_invitation_desc('骑士', LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS['骑士']),
     'epic', class_transfer_use_condition('骑士')),
    ('游侠转职邀请函', 'scroll',
     _class_transfer_invitation_desc('游侠', LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS['游侠']),
     'epic', class_transfer_use_condition('游侠')),
    ('牧师转职邀请函', 'scroll',
     _class_transfer_invitation_desc('牧师', LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS['牧师']),
     'epic', class_transfer_use_condition('牧师')),
    ('法师转职邀请函', 'scroll',
     _class_transfer_invitation_desc('法师', LUCKY_WHEEL_CLASS_TRANSFER_DUNGEONS['法师']),
     'epic', class_transfer_use_condition('法师')),
    # 传奇（全服唯一，按 LUCKY_WHEEL_LEGENDARY_ROTATION 顺序轮换，抽完进下一档）
    ('深渊统御之刃', 'weapon',
     '深渊酒馆镇馆神兵的复刻级长刃，刃身暗涌深渊纹络，对邪魔与深渊生物有叙事层面的压制力；'
     '仍需谨慎使用，过度依赖会招致更高位存在的注视。全服唯一，被抽走后转盘将切换下一传说奖品。', 'legendary', None),
    ('《裂空斩残卷》', 'scroll',
     '全服唯一的剑战技残卷。探险中于安全节点研读后可一次性领悟战技「裂空斩」——对前方直线上的敌人发动沉重斩击，'
     '适合突破单列阵型；贴身混战或宽刃遮挡时难以施展。研读成功后残卷化作纸灰，不可重复使用。'
     '全服唯一，被抽走后转盘将切换下一传说奖品。', 'legendary', None),
    # 稀有
    ('深渊镀银护腕', 'armor',
     '深渊铁匠铺出口的镀银护腕，能挡开轻度劈砍与擦伤；对重击与穿刺防护有限，久戴会磨掉镀层。', 'rare', None),
    ('破魔护身符', 'special',
     '刻有反咒符文的银质护符，佩戴时对轻度诅咒与邪术有缓冲；对高阶禁咒几乎无效。', 'rare', None),
    ('探险家指南针', 'special',
     '经老练向导校准的罗盘，在迷宫与迷雾中略增辨向把握；极端幻术或空间扭曲时仍会失灵。', 'rare', None),
    ('精钢短剑', 'weapon',
     '锻造精良的短剑，平衡趁手，适合副手或敏捷型冒险者；对重甲仍需要技巧与角度。', 'rare', None),
    ('抗毒面罩', 'armor',
     '浸过药液的布质面罩，可滤除低浓度毒雾与瘴气；高毒环境或魔法毒素仍可能穿透。', 'rare', None),
    ('猎人巡夜斗篷', 'armor',
     '深色连帽斗篷，夜行时略减被察觉概率；强光下与普通斗篷无异，遇火易燃。', 'rare', None),
    ('折叠钢盾', 'shield',
     '可折叠收纳的轻型钢盾，展开后能格挡正面冲击；结构较薄，不宜硬抗重武器连击。', 'rare', None),
    # 普通（转盘专属，多数偏「没啥用」的杂物）
    ('磨损的酒馆筹码', 'special',
     '一枚磨圆边的木质筹码，上面印着模糊的深渊酒馆徽记；除当纪念品外几乎一无是处。', 'common', None),
    ('半块硬面包', 'consumable',
     '昨天剩下的硬面包，咬起来像啃木屑，勉强能填一点肚子；探险中未必有人愿意吃。', 'common', None),
    ('空药瓶', 'special',
     '洗干净的玻璃小瓶，标签已被撕掉，装不了多少东西，也卖不出价钱。', 'common', None),
    ('褪色红丝带', 'special',
     '曾经系在某人武器上的红丝带，颜色褪旧、边缘起毛；除了怀旧没有实际用途。', 'common', None),
    ('劣质赝品地图', 'scroll',
     '粗制滥造的「藏宝图」，标注的地点自相矛盾，按图走大概率白跑一趟。', 'common', None),
    ('生锈铁钉包', 'material',
     '一小包氧化发黑的铁钉，勉强能钉木板，做武器或护甲则完全不靠谱。', 'common', None),
    ('深渊酒馆宣传单', 'scroll',
     '印着「今日特价炖菜」的皱巴巴传单，字句夸张，对探险毫无帮助。', 'common', None),
    ('赌坊欠条废纸', 'scroll',
     '某人输钱后撕碎又拼不回去的欠条残片，上面的数字已无法辨认。', 'common', None),
    ('幸运硬币', 'special',
     '一枚磨得发亮的旧硬币，据说能稍稍扭转霉运；效果微妙，也许只是心理安慰。', 'common', None),
    ('粗制磨刀石', 'consumable',
     '粗糙的砥石，可临时恢复兵刃锋利度；一次使用后磨损殆尽。', 'common', None),
    # 普通·探险可用（弱于商店货，叙事触发清晰：暗道照明 / 越障 / 脱身）
    ('浸油劣火炬', 'consumable',
     '短木柄末端裹浸油碎布，点燃后可照亮一小段无灯暗道或坑道；烟大呛人，风一吹或溅水即熄，燃尽即丢。', 'common', None),
    ('钩爪短绳', 'special',
     '约五米粗麻绳配生锈双钩，可抛挂低矮岩檐、横越窄沟或拖开轻便障碍；挂钩易滑、绳体用过一次便磨断报废。', 'common', None),
    ('石灰粉包', 'consumable',
     '一小袋干燥石灰粉，近身抛撒可呛盲贴脸敌人、争取撤步或绕过看守；逆风与开阔处几乎无效，一包一次。', 'common', None),
]

# 传说奖池轮换顺序（全服唯一，抽中一件后 index +1）
LUCKY_WHEEL_LEGENDARY_ROTATION = [
    '深渊统御之刃',
    '《裂空斩残卷》',
]
# 轮换抽完后仍可重复抽到的传说保底（非全服唯一）
LUCKY_WHEEL_LEGENDARY_FALLBACK = '祛厄引路符'
LUCKY_WHEEL_WORLD_UNIQUE_NAMES = frozenset(LUCKY_WHEEL_LEGENDARY_ROTATION)

# 传说奖品附加展示元数据（不进 DB，仅 API 预览）
LUCKY_WHEEL_LEGENDARY_META = {
    '深渊统御之刃': {'item_kind': 'weapon'},
    '《裂空斩残卷》': {'item_kind': 'skill_book', 'grants_skill': '裂空斩'},
}

# 各稀有度奖池物品名（已有图鉴的商店物品也可直接引用；传说走 LUCKY_WHEEL_LEGENDARY_ROTATION）
LUCKY_WHEEL_POOL = {
    'legendary': [],
    'epic': [
        '骑士转职邀请函', '游侠转职邀请函', '牧师转职邀请函', '法师转职邀请函',
    ],
    'rare': [
        '深渊镀银护腕', '破魔护身符', '探险家指南针', '精钢短剑',
        '抗毒面罩', '猎人巡夜斗篷', '折叠钢盾',
    ],
    'common': [
        '磨损的酒馆筹码', '半块硬面包', '空药瓶', '褪色红丝带', '劣质赝品地图',
        '生锈铁钉包', '深渊酒馆宣传单', '赌坊欠条废纸', '幸运硬币', '粗制磨刀石',
        '浸油劣火炬', '钩爪短绳', '石灰粉包',
        '初级治疗药水', '急救绷带', '燃烧瓶',
    ],
}


def seed_lucky_wheel_catalog_items() -> int:
    """补齐幸运大转盘奖池图鉴条目，返回本次新增条数。"""
    names = [row[0] for row in S1_LUCKY_WHEEL_EXCLUSIVE_ITEMS]
    names.append(LUCKY_WHEEL_TICKET_ITEM_NAME)
    existing = {
        i.name: i
        for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    for row in S1_LUCKY_WHEEL_EXCLUSIVE_ITEMS:
        name, type_, desc, rarity = row[0], row[1], row[2], row[3]
        use_condition = row[4] if len(row) > 4 else None
        world_unique = name in LUCKY_WHEEL_WORLD_UNIQUE_NAMES
        item = existing.get(name)
        if item is None:
            to_add.append(AdventurerS1Item(
                name=name, type=type_, description=desc, rarity=rarity,
                is_starter=False, use_condition=use_condition,
                world_unique=world_unique,
            ))
        else:
            if item.type != type_:
                item.type = type_
            if item.description != desc:
                item.description = desc
            if item.rarity != rarity:
                item.rarity = rarity
            if item.use_condition != use_condition:
                item.use_condition = use_condition
            if bool(item.world_unique) != world_unique:
                item.world_unique = world_unique
    ticket = existing.get(LUCKY_WHEEL_TICKET_ITEM_NAME)
    ticket_desc = (
        '可在幸运大转盘抽奖一次。'
    )
    if ticket is None:
        to_add.append(AdventurerS1Item(
            name=LUCKY_WHEEL_TICKET_ITEM_NAME, type='scroll', description=ticket_desc,
            rarity='common', is_starter=False, use_condition=None,
        ))
    elif ticket.description != ticket_desc:
        ticket.description = ticket_desc
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def seed_class_transfer_exchange_catalog_items() -> int:
    """补齐秘境收藏馆专属转职邀请函；这些邀请函不进入幸运大转盘奖池。"""
    names = [row[0] for row in S1_CLASS_TRANSFER_EXCHANGE_ITEMS]
    existing = {
        item.name: item
        for item in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    for name, type_, desc, rarity, use_condition in S1_CLASS_TRANSFER_EXCHANGE_ITEMS:
        item = existing.get(name)
        if item is None:
            to_add.append(AdventurerS1Item(
                name=name,
                type=type_,
                description=desc,
                rarity=rarity,
                is_starter=False,
                use_condition=use_condition,
                world_unique=False,
            ))
            continue
        if item.type != type_:
            item.type = type_
        if item.description != desc:
            item.description = desc
        if item.rarity != rarity:
            item.rarity = rarity
        if item.use_condition != use_condition:
            item.use_condition = use_condition
        if bool(item.world_unique):
            item.world_unique = False
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def seed_task_catalog_items() -> int:
    """补齐任务/活动相关图鉴物品，返回本次新增条数。"""
    from plugins.adventurer_s1.models import (
        AdventurerS1Equipment, AdventurerS1Item, AdventurerS1WarehouseItem,
    )

    for legacy_name in _LEGACY_TREASURE_MAP_ITEM_NAMES:
        if legacy_name == TREASURE_MAP_ITEM_NAME:
            continue
        legacy = AdventurerS1Item.query.filter_by(name=legacy_name).first()
        if legacy is not None:
            legacy.name = TREASURE_MAP_ITEM_NAME
        AdventurerS1Equipment.query.filter_by(name=legacy_name).update(
            {'name': TREASURE_MAP_ITEM_NAME}, synchronize_session=False,
        )
        AdventurerS1WarehouseItem.query.filter_by(name=legacy_name).update(
            {'name': TREASURE_MAP_ITEM_NAME}, synchronize_session=False,
        )
    db_manager.commit()

    names = [row[0] for row in S1_TASK_CATALOG_ITEMS]
    existing = {
        i.name: i
        for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    map_use_condition = treasure_map_use_condition()
    for name, type_, desc, rarity in S1_TASK_CATALOG_ITEMS:
        item = existing.get(name)
        if name == TREASURE_MAP_ITEM_NAME:
            use_condition = map_use_condition
        elif name == BIRTHDAY_CAKE_ITEM_NAME:
            use_condition = birthday_cake_use_condition()
        else:
            use_condition = None
        if item is None:
            to_add.append(AdventurerS1Item(
                name=name, type=type_, description=desc, rarity=rarity,
                is_starter=False, use_condition=use_condition,
            ))
        else:
            if item.type != type_:
                item.type = type_
            if item.description != desc:
                item.description = desc
            if item.rarity != rarity:
                item.rarity = rarity
            if use_condition is not None and item.use_condition != use_condition:
                item.use_condition = use_condition
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def special_dungeon_ticket_desc(dungeon_name: str, flavor: str) -> str:
    return (
        f'{flavor}'
        f'持有此凭证的冒险者方可手动组建前往【{dungeon_name}】的小队；'
        f'小队出发时暂扣一张；若因系统异常未能开启冒险，将自动退还门票'
        f'（随身已满时进入仓库）。'
    )


def seed_special_dungeon_ticket_items() -> int:
    """幂等补齐门票制特殊副本的门票图鉴条目（转职试炼凭证 / 顿悟之钥）。

    门票的 use_condition 标记为 special_dungeon_ticket：纯后端凭证，发车暂扣、
    AI 终态生成失败返还，后端逐成员掷骰；不进入剧情 AI 视野
    （见 s1_item_use_condition_service）。
    """
    from sqlalchemy import or_
    from plugins.adventurer_s1.models import (
        AdventurerS1Equipment,
        AdventurerS1Item,
        AdventurerS1WarehouseItem,
    )
    from plugins.adventurer_s1.services.adventurer_s1_config_service import (
        S1_SPECIAL_DUNGEONS,
    )

    names = [d['ticket_item'] for d in S1_SPECIAL_DUNGEONS]
    existing = {
        i.name: i
        for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    to_add = []
    for dungeon in S1_SPECIAL_DUNGEONS:
        name = dungeon['ticket_item']
        ticket_rarity = str(dungeon.get('ticket_rarity') or 'epic').strip().lower()
        desc = special_dungeon_ticket_desc(dungeon['name'], dungeon.get('flavor', ''))
        use_condition = {
            'item_kind': 'special_dungeon_ticket',
            'special_dungeon': dungeon['name'],
            'text_locked': True,
        }
        item = existing.get(name)
        if item is None:
            to_add.append(AdventurerS1Item(
                name=name, type='ticket', description=desc, rarity=ticket_rarity,
                is_starter=False, use_condition=use_condition,
            ))
        else:
            if item.type != 'ticket':
                item.type = 'ticket'
            if item.description != desc:
                item.description = desc
            if item.rarity != ticket_rarity:
                item.rarity = ticket_rarity
            if item.use_condition != use_condition:
                item.use_condition = use_condition
        for instance_model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            instance_model.query.filter(
                instance_model.name == name,
                or_(
                    instance_model.custom_desc.is_(None),
                    instance_model.custom_desc != desc,
                ),
            ).update({'custom_desc': desc}, synchronize_session=False)
    if to_add:
        db_manager.add_all(to_add, flush=False)
    db_manager.commit()
    return len(to_add)


def seed_hidden_boss_clue_items() -> int:
    """幂等同步隐藏 Boss 线索图鉴（Boss 已讨伐的线索降为 common）。"""
    from plugins.adventurer_s1.models import AdventurerS1Item
    from plugins.adventurer_s1.services.s1_hidden_boss_service import S1HiddenBossService

    existing = {
        row.name: row
        for row in AdventurerS1Item.query.all()
    }
    added = 0
    updated = 0
    for name, type_, desc, rarity, use_condition in S1HiddenBossService.iter_catalog_clue_items():
        row = existing.get(name)
        if row is None:
            db_manager.add(AdventurerS1Item(
                name=name,
                type=type_,
                description=desc,
                rarity=rarity,
                is_starter=False,
                use_condition=use_condition,
            ))
            added += 1
            continue
        changed = False
        if row.type != type_:
            row.type = type_
            changed = True
        if row.description != desc:
            row.description = desc
            changed = True
        if row.rarity != rarity:
            row.rarity = rarity
            changed = True
        if row.use_condition != use_condition:
            row.use_condition = use_condition
            changed = True
        if changed:
            updated += 1
    db_manager.commit()
    S1HiddenBossService.sync_clue_catalog_rarity()
    return added + updated


def seed_herbalist_shop_items() -> int:
    """为草药店补齐商品：图鉴条目 + 在售记录（幂等），返回本次新增条数。"""
    from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1ShopItem

    names = [item[0] for item in S1_HERBALIST_ITEMS]
    existing_items = {
        i.name: i for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    existing_shop_ids = {
        s.item_id for s in AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_([i.id for i in existing_items.values()])
        ).all()
    }

    to_add_items = []
    to_add_shop = []
    price_by_name = {item[0]: item[4] for item in S1_HERBALIST_ITEMS}
    updated_prices = 0
    for name, type_, desc, rarity, price, stock in S1_HERBALIST_ITEMS:
        item = existing_items.get(name)
        if item is None:
            item = AdventurerS1Item(name=name, type=type_, description=desc, rarity=rarity)
            to_add_items.append(item)
            # flush 之后 item.id 才可用，先记下来后面批量处理
            to_add_shop.append((item, price, stock))
        elif item.id not in existing_shop_ids:
            to_add_shop.append((item, price, stock))

    if to_add_items:
        db_manager.add_all(to_add_items, flush=True)

    shop_records = []
    for item, price, stock in to_add_shop:
        shop_records.append(AdventurerS1ShopItem(item_id=item.id, price=price, stock=stock))
    if shop_records:
        db_manager.add_all(shop_records, flush=False)

    # 存量商品同步定价（改 S1_HERBALIST_ITEMS 后重启即可生效）
    if existing_items:
        id_to_name = {item.id: name for name, item in existing_items.items()}
        shop_rows = AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_(id_to_name.keys()),
        ).all()
        for shop_row in shop_rows:
            target_price = price_by_name.get(id_to_name.get(shop_row.item_id))
            if target_price is not None and shop_row.price != target_price:
                shop_row.price = target_price
                updated_prices += 1

    if to_add_items or shop_records or updated_prices:
        db_manager.commit()

    return len(to_add_items) + len(shop_records) + updated_prices


def restock_herbalist_items() -> int:
    """补满草药店商品库存，返回被补货的商品数。"""
    from plugins.adventurer_s1.models import AdventurerS1ShopItem, AdventurerS1Item

    names = [item[0] for item in S1_HERBALIST_ITEMS]
    max_stock = {item[0]: item[5] for item in S1_HERBALIST_ITEMS}

    items = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    id_to_name = {i.id: i.name for i in items}
    if not id_to_name:
        return 0

    shop_items = AdventurerS1ShopItem.query.filter(
        AdventurerS1ShopItem.item_id.in_(id_to_name.keys())
    ).all()

    restocked = 0
    for si in shop_items:
        name = id_to_name.get(si.item_id)
        target = max_stock.get(name)
        if target and si.stock < target:
            si.stock = target
            restocked += 1

    if restocked:
        db_manager.commit()
    return restocked


# ── 军需店初始商品 ─────────────────────────────────────────────────────────
# 出售创建期可选的武器/盾牌/防具（与 STARTER_ITEMS 同名共用图鉴），装备损坏后可补购。
# 价格参考 S1 初始金币 100；库存适中，全体冒险者共享限量。
# 格式：(名称, 价格, 满库存) → 展开为 S1_ARMORY_ITEMS 六元组
_ARMORY_SHOP_LISTINGS = [
    # 武器
    ('新兵铁剑', 25, 10),
    ('猎人短弓', 20, 10),
    ('伐木巨斧', 28, 8),
    ('双生匕首', 12, 15),
    ('学徒法杖', 22, 8),
    # 盾牌
    ('步兵圆盾', 18, 10),
    ('守卫塔盾', 26, 6),
    ('刺客小圆盾', 14, 12),
    ('荆棘藤盾', 16, 10),
    # 防具
    ('皮革轻甲', 15, 12),
    ('链甲衫', 22, 8),
    ('学徒法袍', 18, 10),
    ('斥候轻装', 12, 15),
]

# 军需店消耗品（非 STARTER_ITEMS 图鉴，格式同 S1_HERBALIST_ITEMS）
_ARMORY_CONSUMABLE_ITEMS = [
    ('箭矢', 'consumable',
     '铁头木杆箭，按组出售（每组 10 支），供短弓猎手补充弹药；射出后通常无法回收。', 'common', 12, 99),
]

# 军需店/草药店单次购买入账数量（默认 1；键为物品名）
S1_SHOP_PURCHASE_QUANTITY = {
    '箭矢': 10,
    '小鱼干': 3,
}


def is_quantity_stack_catalog_item(item_name: str) -> bool:
    """堆叠型消耗品：剩余量走 quantity，不设 uses_left（供 AI 与结算一致）。"""
    return int(S1_SHOP_PURCHASE_QUANTITY.get(item_name or '', 1)) > 1


def default_catalog_item_uses_left(item_name: str, item_type: str):
    """图鉴物品默认 uses_left；堆叠型返回 None，单次消耗品返回 1。"""
    if is_quantity_stack_catalog_item(item_name):
        return None
    if is_forge_supply_item(item_name):
        return 1
    if item_type in S1_CONSUMABLE_ITEM_TYPES:
        return 1
    return None

_STARTER_BY_NAME = {name: (type_, desc, rarity) for name, type_, desc, rarity in STARTER_ITEMS}
S1_ARMORY_ITEMS = [
    (name, _STARTER_BY_NAME[name][0], _STARTER_BY_NAME[name][1], _STARTER_BY_NAME[name][2], price, stock)
    for name, price, stock in _ARMORY_SHOP_LISTINGS
    if name in _STARTER_BY_NAME and _STARTER_BY_NAME[name][0] in S1_ARMORY_STARTER_TYPES
] + list(_ARMORY_CONSUMABLE_ITEMS)

ARMORY_ITEM_NAMES = frozenset(item[0] for item in S1_ARMORY_ITEMS)


# ── 熔炉杂货铺初始商品 ─────────────────────────────────────────────────────
# 残次赌材：单价 45~50 金，对齐铁匠铺单次 200 金锻造成本下的「博一把」定位；
# 作铁匠铺辅材时失败率显著高于普通融合，成功则小概率附上对应元素/特性（见 blacksmith addendum）。
# 格式：(名称, 类型, 描述, 稀有度, 价格, 满库存)
S1_FORGE_SUPPLY_ITEMS = [
    ('残次火焰结晶', 'material',
     '内部火纹断续、杂质多的低品火晶，只能作铁匠铺辅材赌一把；多数时候淬火失稳而锻造失败，'
     '极少数情况下能与武器或防具融合，附上微弱火焰之力（略增对寒系目标的灼烧感）。', 'common', 50, 12),
    ('残次寒霜结晶', 'material',
     '表面霜裂、灵韵涣散的寒晶碎块，作辅材时极易因温差碎裂导致锻造失败；'
     '若与金属装备高度契合地融合成功，可能附上微弱寒霜之力（略缓敌人攻势、触感冰凉）。', 'common', 50, 12),
    ('裂纹风语石', 'material',
     '布满细裂的风元素石屑，结构不稳，入炉常散作飞灰而失败；'
     '小概率成功时可令装备略轻、挥动更灵（叙事层面的风语/敏捷感，非奇迹级加速）。', 'common', 45, 14),
    ('浑浊大地碎核', 'material',
     '含泥杂质的土元素核渣，重而涩，与精细装备难相融，失败居多；'
     '侥幸成功时或使装备更沉更韧，略增格挡与耐损的叙事质感。', 'common', 45, 14),
    ('燃烧瓶', 'consumable',
     '简易玻璃瓶装引燃剂，摔碎后可短时引火照明或威慑小型敌人；一次用完即失效。', 'common', 15, 20),
]

FORGE_SUPPLY_ITEM_NAMES = frozenset(item[0] for item in S1_FORGE_SUPPLY_ITEMS)
# 铁匠铺赌材（排除一次性杂物）
FORGE_SUPPLY_FORGE_MATERIAL_NAMES = frozenset(
    name for name, type_, *_rest in S1_FORGE_SUPPLY_ITEMS if type_ == 'material'
)


def is_forge_supply_item(name) -> bool:
    """判定一个物品名是否属于熔炉杂货铺白名单。"""
    return bool(name) and name in FORGE_SUPPLY_ITEM_NAMES


def is_forge_supply_forge_material(name) -> bool:
    """熔炉杂货铺中可用于铁匠铺锻造的赌材（非燃烧瓶等杂物）。"""
    return bool(name) and name in FORGE_SUPPLY_FORGE_MATERIAL_NAMES


def is_armory_item(name) -> bool:
    """判定一个物品名是否属于军需店白名单。"""
    return bool(name) and name in ARMORY_ITEM_NAMES


def get_s1_offline_shop_item_names() -> frozenset:
    """草药店 + 军需店 + 熔炉杂货铺线下商店白名单并集（用于清理 legacy 误入的商店行）。"""
    return HERBALIST_ITEM_NAMES | ARMORY_ITEM_NAMES | FORGE_SUPPLY_ITEM_NAMES


def get_s1_shop_catalog_price(shop_code: str, item_name: str):
    """返回 S1 线下商店的权威定价；非该店商品返回 None。"""
    if shop_code == 'herbalist_shop':
        return next((p for n, *_m, p, _s in S1_HERBALIST_ITEMS if n == item_name), None)
    if shop_code == 'armory_shop':
        return next((p for n, *_m, p, _s in S1_ARMORY_ITEMS if n == item_name), None)
    if shop_code == 'forge_supply_shop':
        return next((p for n, *_m, p, _s in S1_FORGE_SUPPLY_ITEMS if n == item_name), None)
    return None


def get_s1_shop_catalog_purchase_quantity(shop_code: str, item_name: str) -> int:
    """返回单次购买入账的 quantity（默认 1）。"""
    if get_s1_shop_catalog_price(shop_code, item_name) is None:
        return 1
    return int(S1_SHOP_PURCHASE_QUANTITY.get(item_name, 1))


def get_s1_any_shop_catalog_price(item_name: str):
    """草药店与军需店权威定价的最低价；非商店商品返回 None（收购商行防套利 cap 用）。"""
    prices = []
    for shop_code in ('herbalist_shop', 'armory_shop', 'forge_supply_shop'):
        p = get_s1_shop_catalog_price(shop_code, item_name)
        if p is not None:
            prices.append(int(p))
    return min(prices) if prices else None


def get_s1_any_shop_catalog_purchase_quantity(item_name: str) -> int:
    """返回任意商店中该物品的单次购买入账数量（默认 1）。"""
    return int(S1_SHOP_PURCHASE_QUANTITY.get(item_name, 1))


def cleanup_orphan_shop_listings() -> int:
    """删除不在 S1 线下商店白名单内的在售记录（多为 legacy 赛季误入的高价货）。"""
    from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1ShopItem, AdventurerS1BuildingTemplate
    from plugins.adventurer_s1.services.s1_limited_shop_service import limited_shop_catalog

    allowed = set(get_s1_offline_shop_item_names())
    # 活动内容只存数据库；重启清理不得删除管理员配置的限时商品。
    for template in AdventurerS1BuildingTemplate.query.filter_by(handler_key='limited_shop').all():
        catalog = limited_shop_catalog(template)
        if catalog:
            allowed.update(catalog[2])
    orphans = (
        AdventurerS1ShopItem.query
        .join(AdventurerS1Item, AdventurerS1ShopItem.item_id == AdventurerS1Item.id)
        .filter(~AdventurerS1Item.name.in_(allowed))
        .all()
    )
    if not orphans:
        return 0
    for row in orphans:
        ok, err = db_manager.delete_instance(row, flush=False)
        if not ok:
            raise RuntimeError(err or '删除 orphan shop listing 失败')
    db_manager.commit()
    return len(orphans)


def seed_armory_shop_items() -> int:
    """为军需店补齐商品：图鉴条目 + 在售记录（幂等），返回本次新增条数。"""
    from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1ShopItem

    names = [item[0] for item in S1_ARMORY_ITEMS]
    existing_items = {
        i.name: i for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    existing_shop_ids = {
        s.item_id for s in AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_([i.id for i in existing_items.values()])
        ).all()
    }

    to_add_items = []
    to_add_shop = []
    price_by_name = {item[0]: item[4] for item in S1_ARMORY_ITEMS}
    updated_prices = 0
    for name, type_, desc, rarity, price, stock in S1_ARMORY_ITEMS:
        item = existing_items.get(name)
        if item is None:
            item = AdventurerS1Item(name=name, type=type_, description=desc, rarity=rarity)
            to_add_items.append(item)
            to_add_shop.append((item, price, stock))
        elif item.id not in existing_shop_ids:
            to_add_shop.append((item, price, stock))

    if to_add_items:
        db_manager.add_all(to_add_items, flush=True)

    shop_records = []
    for item, price, stock in to_add_shop:
        shop_records.append(AdventurerS1ShopItem(item_id=item.id, price=price, stock=stock))
    if shop_records:
        db_manager.add_all(shop_records, flush=False)

    if existing_items:
        id_to_name = {item.id: name for name, item in existing_items.items()}
        shop_rows = AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_(id_to_name.keys()),
        ).all()
        for shop_row in shop_rows:
            target_price = price_by_name.get(id_to_name.get(shop_row.item_id))
            if target_price is not None and shop_row.price != target_price:
                shop_row.price = target_price
                updated_prices += 1

    if to_add_items or shop_records or updated_prices:
        db_manager.commit()

    return len(to_add_items) + len(shop_records) + updated_prices


def restock_armory_items() -> int:
    """补满军需店商品库存，返回被补货的商品数。"""
    from plugins.adventurer_s1.models import AdventurerS1ShopItem, AdventurerS1Item

    names = [item[0] for item in S1_ARMORY_ITEMS]
    max_stock = {item[0]: item[5] for item in S1_ARMORY_ITEMS}

    items = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    id_to_name = {i.id: i.name for i in items}
    if not id_to_name:
        return 0

    shop_items = AdventurerS1ShopItem.query.filter(
        AdventurerS1ShopItem.item_id.in_(id_to_name.keys())
    ).all()

    restocked = 0
    for si in shop_items:
        name = id_to_name.get(si.item_id)
        target = max_stock.get(name)
        if target and si.stock < target:
            si.stock = target
            restocked += 1

    if restocked:
        db_manager.commit()
    return restocked


def seed_forge_supply_shop_items() -> int:
    """为熔炉杂货铺补齐商品：图鉴条目 + 在售记录（幂等），返回本次新增条数。"""
    from plugins.adventurer_s1.models import AdventurerS1Item, AdventurerS1ShopItem

    names = [item[0] for item in S1_FORGE_SUPPLY_ITEMS]
    existing_items = {
        i.name: i for i in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    }
    existing_shop_ids = {
        s.item_id for s in AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_([i.id for i in existing_items.values()])
        ).all()
    }

    to_add_items = []
    to_add_shop = []
    price_by_name = {item[0]: item[4] for item in S1_FORGE_SUPPLY_ITEMS}
    updated_prices = 0
    for name, type_, desc, rarity, price, stock in S1_FORGE_SUPPLY_ITEMS:
        item = existing_items.get(name)
        if item is None:
            item = AdventurerS1Item(name=name, type=type_, description=desc, rarity=rarity)
            to_add_items.append(item)
            to_add_shop.append((item, price, stock))
        elif item.id not in existing_shop_ids:
            to_add_shop.append((item, price, stock))

    if to_add_items:
        db_manager.add_all(to_add_items, flush=True)

    shop_records = []
    for item, price, stock in to_add_shop:
        shop_records.append(AdventurerS1ShopItem(item_id=item.id, price=price, stock=stock))
    if shop_records:
        db_manager.add_all(shop_records, flush=False)

    if existing_items:
        id_to_name = {item.id: name for name, item in existing_items.items()}
        shop_rows = AdventurerS1ShopItem.query.filter(
            AdventurerS1ShopItem.item_id.in_(id_to_name.keys()),
        ).all()
        for shop_row in shop_rows:
            target_price = price_by_name.get(id_to_name.get(shop_row.item_id))
            if target_price is not None and shop_row.price != target_price:
                shop_row.price = target_price
                updated_prices += 1

    if to_add_items or shop_records or updated_prices:
        db_manager.commit()

    return len(to_add_items) + len(shop_records) + updated_prices


def restock_forge_supply_items() -> int:
    """补满熔炉杂货铺商品库存，返回被补货的商品数。"""
    from plugins.adventurer_s1.models import AdventurerS1ShopItem, AdventurerS1Item

    names = [item[0] for item in S1_FORGE_SUPPLY_ITEMS]
    max_stock = {item[0]: item[5] for item in S1_FORGE_SUPPLY_ITEMS}

    items = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    id_to_name = {i.id: i.name for i in items}
    if not id_to_name:
        return 0

    shop_items = AdventurerS1ShopItem.query.filter(
        AdventurerS1ShopItem.item_id.in_(id_to_name.keys())
    ).all()

    restocked = 0
    for si in shop_items:
        name = id_to_name.get(si.item_id)
        target = max_stock.get(name)
        if target and si.stock < target:
            si.stock = target
            restocked += 1

    if restocked:
        db_manager.commit()
    return restocked


# ── 深渊食堂菜单种子 ──────────────────────────────────────────────────────────

# (code, 名称, 图标, 描述, 金币, sort_order, buffs[(stat, amount), ...])
DINING_MENU_ITEMS = [
    ('plain_porridge', '白粥', '🥣', '温热的白粥，微微壮胆。', 15, 1, [('max_hp', 5)]),
    ('barley_bread', '大麦面包', '🍞', '刚出炉的面包，讨一点好彩头。', 20, 2, [('luck', 1)]),
    ('ginger_tea', '姜糖热饮', '☕', '驱寒暖身，血气壮一些。', 28, 3, [('max_hp', 8)]),
    ('scholar_tea_eggs', '学者茶蛋', '🥚', '茶叶入味，思虑更敏。', 32, 4, [('intelligence', 1)]),
    ('honey_oat_cake', '蜂蜜燕麦糕', '🍯', '甜香扎实的小糕点，路上多攒些见识。', 34, 5, [('exp_pct', 10)]),
    ('hunter_stew', '猎人炖肉', '🍖', '兽肉炖得软烂，下一战更有劲。', 35, 6, [('strength', 1)]),
    ('stream_fish_soup', '溪鱼鲜羹', '🐟', '河鲜清羹，身法更灵便。', 35, 7, [('agility', 1)]),
    ('forest_mushroom_soup', '野菌浓汤', '🍄', '菌菇香气，头脑清明。', 35, 8, [('intelligence', 1)]),
    ('lucky_pie', '幸运馅饼', '🥧', '内馅神秘，好运更足一些。', 38, 9, [('luck', 2)]),
    ('ranger_lunch', '游侠便当', '🎒', '轻装口粮，身手与运气都沾光。', 42, 10, [('agility', 1), ('luck', 1)]),
    ('grilled_ribs', '碳烤肋排', '🥩', '焦香肋排，力气与气血同补。', 45, 11, [('strength', 1), ('max_hp', 5)]),
    ('vitality_broth', '补血浓汤', '🫕', '浓汤暖身，血槽更厚。', 50, 12, [('max_hp', 10)]),
    ('star_sugar_custard', '星糖炖奶', '🍮', '热奶里化着细碎星糖，归来时多悟一分。', 52, 13, [('exp_pct', 20)]),
    ('adventurer_platter', '勇者拼盘', '🍱', '多样搭配，气血与底气都更足。', 55, 14, [('max_hp', 12), ('strength', 1)]),
    ('eleanor_mist_antidote_soup', '月见草抗瘴汤', '🌿', '埃莉诺用迷雾沼泽带回的月见草熬制的抗瘴热汤，头脑清明、气血更稳。', 48, 15, [('intelligence', 1), ('max_hp', 8)]),
    ('eleanor_breath_tea', '埃莉诺滤息茶', '🍵', '老药师调制的滤息草本茶，温胃顺气，下一趟探险更耐环境侵袭。', 40, 16, [('max_hp', 10)]),
]


def _dining_buffs_to_json(buffs: list[tuple[str, int]]) -> list[dict]:
    return [{'stat': stat, 'amount': int(amount)} for stat, amount in buffs]


def seed_dining_menu_items() -> int:
    """补齐深渊食堂菜单，并同步已有餐品的数值/文案，返回本次新增条数。"""
    from plugins.adventurer_s1.models import AdventurerS1DiningMenuItem

    existing_rows = AdventurerS1DiningMenuItem.query.all()
    existing_map = {row.code: row for row in existing_rows}
    to_add = []
    updated = 0
    for code, name, icon, desc, gold, sort_order, buffs in DINING_MENU_ITEMS:
        buffs_json = _dining_buffs_to_json(buffs)
        primary_stat = buffs_json[0]['stat']
        primary_amount = buffs_json[0]['amount']
        if code in existing_map:
            row = existing_map[code]
            changed = False
            for field, val in (
                ('name', name), ('icon', icon), ('description', desc),
                ('gold_cost', gold), ('buff_stat', primary_stat),
                ('buff_amount', primary_amount), ('buffs', buffs_json),
                ('sort_order', sort_order),
            ):
                if getattr(row, field) != val:
                    setattr(row, field, val)
                    changed = True
            if changed:
                updated += 1
            continue
        to_add.append(AdventurerS1DiningMenuItem(
            code=code,
            name=name,
            icon=icon,
            description=desc,
            gold_cost=gold,
            buff_stat=primary_stat,
            buff_amount=primary_amount,
            buffs=buffs_json,
            sort_order=sort_order,
            is_active=True,
        ))
    if to_add:
        db_manager.add_all(to_add, flush=False)
    if to_add or updated:
        db_manager.commit()
    return len(to_add)


def seed_s1_currencies() -> int:
    """为现有冒险者补齐货币记录，返回本次新增条数。新冒险者由 S1CurrencyService.init_currency() 初始化。"""
    from flask import current_app
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Currency

    currency_types = current_app.config.get('ADVENTURER_S1_CURRENCY_TYPES', {})
    if not currency_types:
        return 0

    all_adventurers = AdventurerS1.query.all()
    if not all_adventurers:
        return 0

    existing = set()
    for c in AdventurerS1Currency.query.all():
        existing.add((c.adventurer_id, c.currency_code))

    to_add = []
    for adv in all_adventurers:
        for code, info in currency_types.items():
            if (adv.id, code) in existing:
                continue
            initial = 0.0
            if code == 'gold':
                initial = round(float(adv.gold or 0), 2)
            else:
                initial = round(float(info.get('initial', 0)), 2)
            to_add.append(AdventurerS1Currency(
                adventurer_id=adv.id, currency_code=code, amount=initial,
            ))

    if to_add:
        db_manager.add_all(to_add, flush=False)
        db_manager.commit()
    return len(to_add)


def sync_gold_currency_with_main() -> int:
    """gold 并轨修复（幂等）：把 Currency 'gold' 余额对齐主表 `AdventurerS1.gold`。

    2026-06-10 之前产出端（探险 / 任务 / 建筑结算）直改主表、消耗端（商店）扣
    Currency 表，两轨漂移；并轨后所有 gold 变动统一走 S1CurrencyService 并自动
    同步主表，本函数只负责把历史漂移一次性拉齐（主表为历史权威——它累计了全部
    产出与 leisure 消费）。差额写 CurrencyLog（source_type='gold_rail_sync'）留审计。
    返回本次修正的记录数。
    """
    from plugins.adventurer_s1.models import (
        AdventurerS1, AdventurerS1Currency, AdventurerS1CurrencyLog,
    )

    main_gold = {
        row[0]: round(float(row[1] or 0), 2)
        for row in AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.gold).all()
    }
    if not main_gold:
        return 0

    fixed = 0
    logs = []
    for rec in AdventurerS1Currency.query.filter_by(currency_code='gold').all():
        target = main_gold.get(rec.adventurer_id)
        if target is None:
            continue
        current = round(float(rec.amount or 0), 2)
        if abs(current - target) < 0.005:
            continue
        rec.amount = target
        logs.append(AdventurerS1CurrencyLog(
            adventurer_id=rec.adventurer_id,
            currency_code='gold',
            change_amount=round(target - current, 2),
            balance_after=target,
            source_type='gold_rail_sync',
            description='gold 双轨并轨：Currency 余额对齐主表',
        ))
        fixed += 1

    if fixed:
        db_manager.add_all(logs, flush=False)
        db_manager.commit()
    return fixed


def migrate_currencies_to_materials() -> int:
    """把历史 wood / stone currency 余额迁移成 MaterialItem，并清零 currency。

    幂等：amount=0 的 currency 记录会被跳过，重复调用安全无副作用。
    场景：阶段 E（材料物品化）落地时执行一次，把旧数据从 currency 体系转出到
    独立的 MaterialItem 库存。新采集走 BuildingSettlementService 直接进
    MaterialItem，不再走 currency。
    返回本次实际迁移的 currency 行数。
    """
    from plugins.adventurer_s1.models import AdventurerS1Currency
    from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService

    LEGACY_CODES = ('wood', 'stone')
    legacy_recs = AdventurerS1Currency.query.filter(
        AdventurerS1Currency.currency_code.in_(LEGACY_CODES),
        AdventurerS1Currency.amount > 0,
    ).all()
    if not legacy_recs:
        return 0

    migrated = 0
    for rec in legacy_recs:
        # 整数化（材料按整数计量；历史 currency 是 Float，按 floor 处理避免凭空多送）
        qty = int(rec.amount)
        if qty <= 0:
            continue
        _, err = S1MaterialService.add(
            rec.adventurer_id, rec.currency_code, qty,
            source_type='migration_from_currency',
            related_id=rec.id,
            related_type='currency_record',
            description='从 currency 迁移到 MaterialItem',
            auto_commit=False,
        )
        if err:
            # 单条失败不阻塞其他迁移；记录后跳过
            import logging
            logging.getLogger(__name__).error(
                '迁移 currency→material 失败: adv=%s code=%s amount=%s err=%s',
                rec.adventurer_id, rec.currency_code, rec.amount, err,
            )
            continue
        # 清零原 currency 余额（保留记录作为审计痕迹，不直接删除）
        rec.amount = 0.0
        migrated += 1

    if migrated > 0:
        db_manager.commit()
    return migrated
