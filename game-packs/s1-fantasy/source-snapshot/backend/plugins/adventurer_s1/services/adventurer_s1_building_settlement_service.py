"""深渊酒馆 S1 建筑结算奖励服务。

冒险者入驻建筑完成后，根据建筑所属分类（或模板自定义配置）发放结算奖励。
结算时机：建筑建造完成、冒险者被召回、精力耗尽自动释放时。

奖励类型（reward_type）：
  - gold        → 加 AdventurerS1.gold
  - hp          → 恢复生命值
  - stamina     → 恢复精力
  - exp         → 加经验值
  - tavern_coin → 加酒馆币（通过 S1CurrencyService）
  - <其他货币>  → 通过 S1CurrencyService.add()
  - none        → 不发放

使用优先级：模板 settlement_config 字段 > 分类默认配置 > 不结算。
"""

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import logging
from typing import Optional, Tuple

from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1BuildingInstance, AdventurerS1BuildingHelper,
)

logger = logging.getLogger(__name__)


# ── 分类默认结算配置 ──────────────────────────────────────────────────────────
# 当建筑模板的 settlement_config 为 None 时使用此表。
#
# 2026-06-10 起全部为 none：默认表曾给 leisure/medical/equipment/market/work 发
# 小额奖励，但默认表没有 usage_stamina_cost / daily_limit / cooldown 等任何使用
# 成本字段，导致「无 settlement_config 的占位建筑（铁匠铺/治疗所/交易市场）」
# 变成零成本无限刷的提款机。现在的规则：**要发奖励就必须在模板 settlement_config
# 里显式声明完整配置（奖励 + 成本 + 限额）**，没有配置 = 用了也没有奖励。

_NO_REWARD = {
    'reward_type': 'none',
    'reward_amount': 0,
    'reward_currency_code': None,
    'description': '',
}

CATEGORY_DEFAULT_REWARDS = {
    'leisure': dict(_NO_REWARD),
    'medical': dict(_NO_REWARD),
    'equipment': dict(_NO_REWARD),
    'training': dict(_NO_REWARD),
    'gathering': dict(_NO_REWARD),
    'market': dict(_NO_REWARD),
    'work': dict(_NO_REWARD),
    'dormitory': dict(_NO_REWARD),
    'general': dict(_NO_REWARD),
}


class BuildingSettlementService:
    """建筑结算奖励统一接口。"""

    # ── 打工属性溢出加成 ────────────────────────────────────────────────────────

    @staticmethod
    def calc_stat_overflow_bonus(adventurer: AdventurerS1, config: dict) -> int:
        """超出属性要求的部分，每 N 点（默认 5）加 1 金币/次；参与计算的属性上限 40。

        例：要求力量≥10，当前力量 15 → +1；19 → 仍 +1；45 → 按 40 计 → +6。
        """
        required_stat = config.get('required_stat')
        required_min = int(config.get('required_stat_min') or 0)
        if not required_stat or required_min <= 0:
            return 0
        if not hasattr(adventurer, required_stat):
            return 0

        stat_cap = int(current_app.config.get('ADVENTURER_S1_WORK_STAT_OVERFLOW_CAP', 40))
        step = int(current_app.config.get('ADVENTURER_S1_WORK_STAT_OVERFLOW_STEP', 5))
        bonus_per_step = int(current_app.config.get('ADVENTURER_S1_WORK_STAT_OVERFLOW_BONUS', 1))
        if step <= 0 or bonus_per_step <= 0:
            return 0

        stat_value = int(getattr(adventurer, required_stat, 0) or 0)
        effective_stat = min(stat_value, stat_cap)
        overflow = max(0, effective_stat - required_min)
        return (overflow // step) * bonus_per_step

    # ── 配置读取 ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_reward_config(building_instance: AdventurerS1BuildingInstance) -> dict:
        """获取建筑的结算奖励配置。

        优先使用模板的 settlement_config 字段；为 None 时回退到分类默认值。
        返回完整 dict，结算字段（reward_type / reward_amount / reward_currency_code /
        stat_name / description）与使用流程字段（usage_stamina_cost /
        usage_duration_minutes / daily_limit / required_stat / required_stat_min）
        都原样透传，供 start_building_usage 等调用方读取。
        """
        template = building_instance.template
        if not template:
            return CATEGORY_DEFAULT_REWARDS['general'].copy()

        # 模板自定义配置优先：直接 copy，保留所有字段（含 usage_* / required_*）
        if template.settlement_config:
            return dict(template.settlement_config)

        # 分类默认
        category = template.category or 'general'
        return CATEGORY_DEFAULT_REWARDS.get(category, CATEGORY_DEFAULT_REWARDS['general']).copy()

    # ── 结算入口 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _get_upgrade_effects(building_instance: AdventurerS1BuildingInstance) -> dict:
        """读取建筑升级的 handler_effects（Lv2+ 才有）。"""
        level = int(getattr(building_instance, 'upgrade_level', 1) or 1)
        if level <= 1 or not building_instance.template:
            return {}
        cfg = building_instance.template.upgrade_config
        if not cfg:
            return {}
        return cfg.get('handler_effects', {}) or {}

    @staticmethod
    def settle(
        adventurer: AdventurerS1,
        building_instance: AdventurerS1BuildingInstance,
        source_type: str = 'building_settle',
    ) -> Tuple[bool, Optional[str]]:
        """为单个冒险者执行建筑结算奖励。

        Args:
            adventurer: 冒险者对象
            building_instance: 建筑实例对象
            source_type: 来源类型（用于货币日志）

        Returns:
            (success, error_message)
        """
        config = BuildingSettlementService.get_reward_config(building_instance)
        reward_type = config.get('reward_type', 'none')
        reward_amount = config.get('reward_amount', 0)
        overflow_bonus = 0

        # 读取升级效果
        upgrade_effects = BuildingSettlementService._get_upgrade_effects(building_instance)

        if reward_type == 'gold':
            overflow_bonus = BuildingSettlementService.calc_stat_overflow_bonus(adventurer, config)
            reward_amount = float(reward_amount) + overflow_bonus

        if reward_type == 'none' or reward_amount <= 0:
            return True, None

        # 升级加成：material
        if reward_type == 'material':
            material_bonus = int(upgrade_effects.get('material_bonus', 0) or 0)
            if material_bonus > 0:
                reward_amount += material_bonus

        # 升级加成：stamina（休闲建筑精力恢复+5）
        if reward_type == 'stamina':
            stamina_bonus = int(upgrade_effects.get('stamina_restore_bonus', 0) or 0)
            if stamina_bonus > 0:
                reward_amount += stamina_bonus

        description = config.get('description', '')
        if overflow_bonus > 0:
            base_desc = description or '建筑结算奖励'
            description = f'{base_desc}（属性溢出 +{overflow_bonus} 金币）'

        ok, err = BuildingSettlementService._apply_reward(
            adventurer, reward_type, reward_amount,
            currency_code=config.get('reward_currency_code'),
            material_name=config.get('reward_material_name'),
            stat_name=config.get('stat_name'),
            source_type=source_type,
            description=description,
            building_instance=building_instance,
        )
        if not ok:
            return ok, err

        # 医院 Lv2：回血的同时恢复精力
        if reward_type == 'hp' and upgrade_effects.get('stamina_on_heal'):
            max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
            adventurer.stamina = min(max_stamina, int((adventurer.stamina or 0) + int(reward_amount)))

        # 升级额外产出材料（如采石场 Lv2 额外给铁矿石）
        bonus_mat = upgrade_effects.get('bonus_material')
        bonus_mat_amount = int(upgrade_effects.get('bonus_material_amount', 0) or 0)
        if bonus_mat and bonus_mat_amount > 0:
            from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
            S1MaterialService.add(
                adventurer.id, bonus_mat, bonus_mat_amount,
                source_type=source_type,
                related_id=related_id,
                related_type='building_instance',
                description=f'升级额外产出',
                auto_commit=False,
            )

        return True, None

    @staticmethod
    def settle_all_helpers(
        building_instance: AdventurerS1BuildingInstance,
        source_type: str = 'building_settle',
    ) -> int:
        """结算建筑内所有在位冒险者，返回成功结算的人数。

        用于建筑建造完成、召回所有冒险者等场景。
        """
        template = building_instance.template
        if not template:
            return 0

        # 查询在位的冒险者（必须 status='building'，排除残留脏数据）
        helpers = AdventurerS1.query.filter_by(
            building_instance_id=building_instance.id,
            status='building',
        ).all()

        settled = 0
        for adv in helpers:
            ok, err = BuildingSettlementService.settle(adv, building_instance, source_type)
            if ok:
                settled += 1
            else:
                logger.warning(
                    '建筑结算失败: adventurer=%s building=%s error=%s',
                    adv.id, building_instance.id, err,
                )

        if settled > 0:
            db_manager.commit()
            logger.info('建筑结算完成: building=%s settled=%d/%d',
                        building_instance.id, settled, len(helpers))

        return settled

    # ── 奖励应用 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_reward(
        adventurer: AdventurerS1,
        reward_type: str,
        amount: float,
        currency_code: Optional[str] = None,
        material_name: Optional[str] = None,
        stat_name: Optional[str] = None,
        source_type: str = 'building_settle',
        description: str = '',
        building_instance: Optional[AdventurerS1BuildingInstance] = None,
    ) -> Tuple[bool, Optional[str]]:
        """根据 reward_type 对冒险者应用奖励。"""
        related_id = building_instance.id if building_instance else None

        try:
            if reward_type == 'gold':
                # gold 走统一货币接口（Currency 表权威 + 自动同步主表 + 审计日志）
                from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
                _, err = S1CurrencyService.add(
                    adventurer.id, 'gold', float(amount),
                    source_type=source_type,
                    related_id=related_id,
                    related_type='building_instance',
                    description=description or '建筑结算奖励',
                    auto_commit=False,
                )
                if err:
                    return False, err

            elif reward_type == 'hp':
                adventurer.hp = min(
                    adventurer.max_hp or 100,
                    int((adventurer.hp or 0) + amount),
                )

            elif reward_type == 'stamina':
                max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
                adventurer.stamina = min(
                    max_stamina,
                    int((adventurer.stamina or 0) + amount),
                )

            elif reward_type == 'exp':
                adventurer.exp = int((adventurer.exp or 0) + amount)

            elif reward_type == 'stat':
                if not stat_name or not hasattr(adventurer, stat_name):
                    return False, f'无效属性: {stat_name}'
                from plugins.adventurer_s1.services.s1_adventurer_stat_service import (
                    S1_BASE_ATTRS, add_s1_attr, add_s1_max_hp, clamp_s1_adventurer_stats,
                )
                if stat_name in S1_BASE_ATTRS:
                    add_s1_attr(adventurer, stat_name, int(amount))
                elif stat_name == 'max_hp':
                    add_s1_max_hp(adventurer, int(amount), sync_hp=True)
                else:
                    current = int(getattr(adventurer, stat_name) or 0)
                    setattr(adventurer, stat_name, current + int(amount))
                clamp_s1_adventurer_stats(adventurer)

            elif reward_type == 'material':
                # 采集类建筑：把材料落到独立的 MaterialItem 库存（不进装备 / 不进副本）
                from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
                code = material_name or 'unknown'
                _, err = S1MaterialService.add(
                    adventurer.id, code, int(amount),
                    source_type=source_type,
                    related_id=related_id,
                    related_type='building_instance',
                    description=description or f'采集所得',
                    auto_commit=False,
                )
                if err:
                    return False, err

            else:
                # 货币类（tavern_coin 或其他自定义货币）
                from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
                code = currency_code or reward_type
                _, err = S1CurrencyService.add(
                    adventurer.id, code, amount,
                    source_type=source_type,
                    related_id=related_id,
                    related_type='building_instance',
                    description=description or f'建筑结算奖励',
                    auto_commit=False,
                )
                if err:
                    return False, err

            return True, None

        except Exception as e:
            logger.error('结算奖励应用失败: adventurer=%s type=%s error=%s', adventurer.id, reward_type, e)
            return False, player_operation_failure(e, '操作失败')
