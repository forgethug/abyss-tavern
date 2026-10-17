"""深渊酒馆 S1 建筑处理器注册表。

handler_key 指向具体处理器，定义建筑在 start/complete 生命周期的自定义逻辑。
无 handler_key 的建筑走 settlement_config / category 默认值。

用法：
    from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_handler
    handler = get_handler(template.handler_key)
    if handler:
        err = handler.can_start(adv, instance, config)
"""
from runtime_core.time import utc_now as _portable_utc_now
import hashlib
import hmac
import logging
import math
from abc import ABC
from datetime import datetime
from typing import Optional, Tuple
from uuid import uuid4

from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
from plugins.adventurer_s1.services.s1_item_assembly_contract import (
    ITEM_ASSEMBLY_INPUT_CONTRACT_VERSION,
    ITEM_ASSEMBLY_LAYOUT_LABELS,
    assembly_layout_position,
    build_assembly_input_slots,
    build_assembly_input_snapshot_hash,
)
from plugins.adventurer_s1.services.s1_ember_kitchen_contract import (
    EMBER_KITCHEN_COOKWARE,
    EMBER_KITCHEN_INPUT_CONTRACT_VERSION,
    build_ember_kitchen_input_slots,
    build_ember_kitchen_input_snapshot_hash,
)

logger = logging.getLogger(__name__)


class BuildingUsageInProgress(str):
    """异步建筑的同一任务已在队列或执行中。"""


class TerminalBuildingUsageError(str):
    """已保存的结算输入失效，继续重试不会自行恢复。"""


class BaseBuildingHandler(ABC):
    """建筑处理器基类。所有方法都是可选覆写的 hook。"""

    # 处理器显示名称（供 GM 前端选择用）
    display_name: str = '未命名'
    # 处理器描述
    description: str = ''
    # 完成结算时必须重新加载的 AdventurerS1 关系。scheduler 候选扫描会 noload 重关系，
    # 由具体 handler 声明实际消费项，避免通用结算按建筑类型写分支。
    completion_relationships: Tuple[str, ...] = ()

    def can_start(self, adv, instance, config) -> Optional[str]:
        """进入使用前的额外校验。返回 None=通过，str=错误信息。"""
        return None

    def on_start(self, adv, instance, config):
        """进入使用时的额外逻辑。"""
        pass

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        """使用完成时的结算逻辑。返回 (ok, err)。

        返回 (True, None) 表示由 handler 处理完毕，跳过默认 settlement。
        返回 (False, None) 表示不做自定义结算，fallback 到默认 settlement。

        extra_params：前端透传的额外参数（如铁匠铺的 equipment_name / material_names）。
        """
        return False, None

    def is_async(self) -> bool:
        """是否为异步 handler。

        异步 handler 的 on_complete 只提交任务、不立即结算；
        真正的结果落地由 AsyncTaskService worker 完成。
        complete_building_usage 会据此跳过 usage_log 记录和冒险者释放。
        """
        return False

    def is_async_for(self, extra_params: dict = None) -> bool:
        """按本次派遣快照判断是否异步；默认沿用 handler 级固定属性。"""
        return self.is_async()

    def on_recall(self, adv, instance, config):
        """冒险者被提前召回时的逻辑。"""
        pass

    def get_reward_preview(self, instance, config) -> dict:
        """前端展示用的奖励预览信息。"""
        return {}


# ── 具体处理器 ──────────────────────────────────────────────────────────────

class SecretAuctionHandler(BaseBuildingHandler):
    """隐秘拍卖行：密封出价，无需派遣冒险者占位。"""
    display_name = '隐秘拍卖行'
    description = '管理员上架拍品，玩家密封出价，到期最高者得；同价流拍。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '拍卖行无需派遣冒险者，请直接在拍卖界面出价'


class HiddenDungeonExchangeHandler(BaseBuildingHandler):
    """秘境收藏馆：固定配方兑换，无需派遣占位。"""
    display_name = '秘境收藏馆'
    description = '收集不同隐藏副本的普通掉落，按固定组合兑换门票、技能卷轴等珍贵物品。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '秘境收藏馆无需派遣冒险者，请直接选择配方兑换'


class HerbalistShopHandler(BaseBuildingHandler):
    """草药店处理器：禁止派遣占位，购买走 buy_from_shop。"""
    display_name = '草药店'
    description = '对接商品购买系统，玩家直接选购，物品进入冒险者随身。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '商店无需派遣冒险者，请直接在购买界面选购'


class ArmoryShopHandler(BaseBuildingHandler):
    """军需店处理器：与草药店类似的商店逻辑，但归属军需店商品白名单。"""
    display_name = '军需店'
    description = '对接商品购买系统（军需店类型），玩家直接选购装备/补给。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '商店无需派遣冒险者，请直接在购买界面选购'


class ForgeSupplyShopHandler(BaseBuildingHandler):
    """熔炉杂货铺处理器：贩售残次赌材与一次性杂物。"""
    display_name = '熔炉杂货铺'
    description = '对接商品购买系统（熔炉杂货铺类型），玩家直接选购残次赌材与杂物。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '商店无需派遣冒险者，请直接在购买界面选购'


class LimitedShopHandler(BaseBuildingHandler):
    """按建筑配置中的时间窗口与商品清单开放的限时摊位。"""
    display_name = '限时摊位'
    description = '在指定时间内开放购买，到期自动停止售卖。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '摊位无需派遣冒险者，请直接在购买界面选购'


class BuybackShopHandler(BaseBuildingHandler):
    """收购商行处理器：禁止派遣占位，出售走 buyback API。"""
    display_name = '收购商行'
    description = '对接物品收购系统，玩家出售随身或仓库物品换取金币。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '收购商行无需派遣冒险者，请在出售界面操作'


class GamblingHallHandler(BaseBuildingHandler):
    """深渊赌坊：8 赌桌座位随时可入座；有荷官在岗方可下注，开奖时无荷官则流局退金。"""
    display_name = '深渊赌坊'
    description = '可随时入座；须有人在赌场荷官岗位打工时方可下注与开奖，荷官离岗则本轮流局退金。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        return True, None


class LuckyWheelHandler(BaseBuildingHandler):
    """幸运大转盘：消耗抽奖券抽取奖池物品，无需派遣占位。"""
    display_name = '幸运大转盘'
    description = '对接抽奖系统，玩家直接抽奖，物品进入冒险者随身或仓库。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '幸运大转盘无需派遣冒险者，请在抽奖界面操作'


class SkillSchoolHandler(BaseBuildingHandler):
    """百艺阁：旧卷传艺同步授予，导师指点异步生成新技能 / 进阶。"""
    display_name = '百艺阁'
    description = '旧卷与练习器具堆满长桌，驻阁导师会因材施教，替来客琢磨合身的本事。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return None

    @staticmethod
    def _release_usage_slot(adv) -> None:
        from plugins.adventurer_s1.services.s1_skill_school_service import (
            S1SkillSchoolService,
        )

        S1SkillSchoolService.release_usage_slot(adv.id)

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService

        stored = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        params = stored or (extra_params or {})
        if S1SkillSchoolService.is_custom_training_extra(params):
            from services.async_task_service import AsyncTaskService

            usage_session_id = str(params.get('usage_session_id') or '').strip()
            if not usage_session_id:
                return False, '这堂课的记档出了岔子，先别继续，稍后再试'
            payload = {
                'adventurer_id': adv.id,
                'building_instance_id': instance.id,
                'usage_session_id': usage_session_id,
            }
            _bind_async_usage_session('ai_skill_training', payload, adv, params)
            if AsyncTaskService.is_task_enqueued_or_running('ai_skill_training', payload):
                return False, BuildingUsageInProgress('导师正在收尾，请等待结果通知')
            if not AsyncTaskService.submit_task('ai_skill_training', payload):
                return False, '导师这会儿还收不了尾，稍后再来看看'
            return True, None

        skill_code = str(params.get('skill_code') or '').strip()
        if not skill_code:
            skill_code = str(stored.get('skill_code') or '').strip()
        _, err = S1SkillSchoolService.grant_skill(adv, skill_code, instance)
        if err:
            return False, err
        self._release_usage_slot(adv)
        return True, None

    def on_recall(self, adv, instance, config):
        self._release_usage_slot(adv)

    def is_async_for(self, extra_params: dict = None) -> bool:
        from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService

        return S1SkillSchoolService.is_custom_training_extra(extra_params)


class TaskBoardHandler(BaseBuildingHandler):
    """任务栏处理器：校验任务栏建筑，on_complete 不结算（奖励来自 complete_task）。"""
    display_name = '任务栏'
    description = '对接任务系统，冒险者进入后可接取任务，使用完成不触发额外结算。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        # 任务建筑的奖励来自任务完成流程
        return True, None


class CampaignHallHandler(BaseBuildingHandler):
    display_name = '战役议事厅'
    description = '查看世界事件进展并组队接取行动。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '请在战役议事厅选择行动并组队'


class DormitoryHandler(BaseBuildingHandler):
    """宿舍楼处理器：宿舍楼有独立的入住/建造流程，不走 start/complete。"""
    display_name = '宿舍楼'
    description = '宿舍楼有独立的入住和建造流程，不使用通用的建筑使用流程。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '宿舍楼不支持直接使用，请通过宿舍区管理'

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        return True, None


class ComingSoonHandler(BaseBuildingHandler):
    """占位建筑处理器：功能尚未实现的建筑（个人交易市场），禁止进入使用。

    这些建筑可以正常建造与展示，但描述里的功能（玩家交易）
    还没有对应系统；在功能落地前拦住使用入口，避免玩家进去空耗或踩到默认结算。
    """
    display_name = '未开放'
    description = '功能开发中的占位建筑，建成后暂不可使用。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        name = instance.template.name if instance.template else '该建筑'
        return f'「{name}」尚未开放使用，敬请期待'

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        # 兜底：即便有人处于使用态，也只释放不结算
        return True, None


class TranscendentAssessmentPreviewHandler(BaseBuildingHandler):
    """越阶评定的静观预览：只供瞻仰，绝不进入通用使用与结算流程。"""

    display_name = '无冕终庭'
    description = '终庭只观察已经完成的功业，当前仅供瞻仰。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '无冕终庭不发布任务，也不接受自荐'

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        # 防御性兜底：不授予奖励，不产生评定结果。
        return True, None


class ContributionMonumentHandler(BaseBuildingHandler):
    """纯共建纪念物：建造完成后只供瞻仰，不进入通用使用/结算流程。"""

    display_name = '纯共建纪念物'
    description = '仅通过捐料和帮建获得贡献度；建成后不提供使用入口或奖励。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        name = instance.template.name if instance.template else '该纪念物'
        return f'「{name}」仅供瞻仰，建成后没有使用奖励'

    def on_complete(
        self, adv, instance, config, extra_params: dict = None,
    ) -> Tuple[bool, Optional[str]]:
        # 防御性兜底：历史或异常使用态只释放，不触发默认结算。
        return True, None


class WorldIntelHandler(BaseBuildingHandler):
    """世界情报建筑占位：先只注册 handler，不向玩家开放模板实例。"""

    display_name = '世界情报库（隐藏占位）'
    description = '未来用于玩家情报建筑；当前仅供管理员后台维护世界情报库，不开放玩家使用。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '世界情报建筑暂未向玩家开放'

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        return True, None


# ── 治疗所 & 铁匠铺：AI 驱动的异步 handler ─────────────────────────────────

def _bind_async_usage_session(
    task_type: str,
    payload: dict,
    adv,
    extra_params: dict | None,
) -> dict:
    """绑定会话号，并把本次终态写入权认领到当前后端启动。"""
    from plugins.adventurer_s1.services.s1_building_ai_ownership_service import (
        stamp_current_building_ai_owner,
    )

    stored, owner_changed = stamp_current_building_ai_owner(adv.building_usage_extra)
    adv.building_usage_extra = stored
    usage_session_id = str(
        stored.get('usage_session_id')
        or (extra_params or {}).get('usage_session_id')
        or ''
    ).strip()
    if usage_session_id:
        payload['usage_session_id'] = usage_session_id
    if owner_changed:
        # 新会话通常没有旧锁；跨 boot 再次进入 complete 时则必须先解除旧
        # worker 的排队锁，否则它会挡住本次启动版本立即补投。
        from services.async_task_service import AsyncTaskService
        AsyncTaskService.force_release_task_lock(task_type, payload)
    return payload


def _has_recoverable_traits(adv) -> bool:
    """检查冒险者是否有可自然恢复的负面特质（rest/sleep 策略）。"""
    for t in adv.traits:
        if t.is_archived:
            continue
        policy = (getattr(t, 'recovery_policy', None) or 'none').strip().lower()
        stage = (getattr(t, 'recovery_stage', None) or '').strip().lower()
        if policy in {'rest', 'sleep'} and stage in {'light', 'medium'}:
            return True
    return False


def _has_healable_traits(adv) -> bool:
    """检查冒险者是否有未归档的特质（具体能否治疗交给 AI 判断）。"""
    for t in adv.traits:
        if not t.is_archived:
            return True
    return False


class HealingHouseHandler(BaseBuildingHandler):
    """治疗所处理器：AI 驱动的伤势治疗。

    冒险者进入治疗所后，AI 根据其负面特质判断哪些肉体伤害可以治愈。
    Lv2 升级后可携带一件治疗物品辅助治疗；治疗成功必扣携带物，失败时是否扣物看 AI item_used。
    治疗结果异步返回：成功则 archived 对应特质，失败则不消耗特质。
    """
    display_name = '治疗所'
    description = '专业的伤势治疗设施，由经验丰富的医师评估并治愈冒险者的肉体伤害（负面特质）。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not _has_healable_traits(adv):
            return '该冒险者没有可治疗的肉体伤害'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        raw_traits = extra_params.get('target_traits') or []
        # 去重 + 校验存在性
        target_traits = list(dict.fromkeys(str(t).strip() for t in raw_traits if t))
        if not target_traits:
            return False, '请指定要治疗的特质'
        if len(target_traits) > 1:
            return False, '单次治疗只能选择 1 个特质'

        existing = {t.name for t in adv.traits}
        for tn in target_traits:
            if tn not in existing:
                return False, f'未找到特质「{tn}」'

        # Lv2 治疗物品（可选）
        item_ref = extra_params.get('item_ref')

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'target_traits': target_traits,
        }
        _bind_async_usage_session('ai_healing_house', payload, adv, extra_params)
        if item_ref:
            payload['item_ref'] = item_ref
        if AsyncTaskService.is_task_enqueued_or_running('ai_healing_house', payload):
            return False, BuildingUsageInProgress('治疗仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_healing_house', payload):
            return False, '治疗暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class ChapelHandler(BaseBuildingHandler):
    """小圣堂处理器：AI 驱动的诅咒/死灵/污染类特质净化。

    由见习修女主持祈祷，AI 根据特质性质判断能否净化。
    因修女力量有限，净化成功率较低。
    """
    display_name = '小圣堂'
    description = '见习修女主持的祈祷室，尝试净化诅咒、死灵、污染类负面特质；修女力量有限，成功率不高。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not _has_healable_traits(adv):
            return '该冒险者没有可净化的特质'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        raw_traits = extra_params.get('target_traits') or []
        target_traits = list(dict.fromkeys(str(t).strip() for t in raw_traits if t))
        if not target_traits:
            return False, '请指定要净化的特质'
        if len(target_traits) > 1:
            return False, '单次祈祷只能选择 1 个特质'

        existing = {t.name for t in adv.traits}
        for tn in target_traits:
            if tn not in existing:
                return False, f'未找到特质「{tn}」'

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'target_traits': target_traits,
        }
        _bind_async_usage_session('ai_chapel', payload, adv, extra_params)
        if AsyncTaskService.is_task_enqueued_or_running('ai_chapel', payload):
            return False, BuildingUsageInProgress('祈祷正在进行中，请等待结果')
        if not AsyncTaskService.submit_task('ai_chapel', payload):
            return False, '祈祷暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class SanatoriumHandler(BaseBuildingHandler):
    """疗养所处理器：按小时租用床位，加速负面特质自然恢复期（2× 有效休息秒数）。"""
    display_name = '疗养所'
    description = '加速特质自然恢复，设有多个床位可按小时租用。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not _has_recoverable_traits(adv):
            return '该冒险者没有可通过休息/静养自然恢复的负面特质'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        # 精力/血量必须在特质恢复之前落账——flush_sanatorium_rest_session 会重置 building_assigned_at
        self._flush_stamina(adv)
        self._flush_hp(adv)
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
            flush_sanatorium_rest_session,
        )
        flush_sanatorium_rest_session(adv)
        return True, None

    def on_recall(self, adv, instance, config):
        # 精力/血量必须在特质恢复之前落账——flush_sanatorium_rest_session 会重置 building_assigned_at
        self._flush_stamina(adv)
        self._flush_hp(adv)
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
            flush_sanatorium_rest_session,
        )
        flush_sanatorium_rest_session(adv)

    @staticmethod
    def _flush_stamina(adv):
        """将疗养期间惰性计算的精力值落账，避免 enter_rest 重置 timer 后丢失。"""
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import flush_stamina_to_db
        flush_stamina_to_db(adv)

    @staticmethod
    def _flush_hp(adv):
        """将疗养期间惰性计算的生命值落账，避免 enter_rest 重置 hp_updated_at 后丢失。"""
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import flush_hp_to_db
        flush_hp_to_db(adv)

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'gold_cost_per_hour': config.get('gold_cost_per_hour', 10),
            'usage_slots': config.get('usage_slots', 4),
            'description': config.get('description', '疗养完成，恢复期加速已计入'),
        }


class HospitalHandler(BaseBuildingHandler):
    """医院处理器：付费快速恢复生命值，支持多床位并发收治；灵体丸丸崽持流星锤协助治疗。"""
    display_name = '医院'
    description = '付费快速恢复生命值，设有多个病床可同时收治伤患。灵体状态的一只丸丸崽还会拿着流星锤在病人旁边帮忙治疗。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        from flask import current_app
        recovery_minutes = int(current_app.config.get('ADVENTURER_S1_DORM_HP_RECOVERY_MINUTES', 10))
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_hp
        current_hp = get_live_hp(adv, recovery_minutes_per_point=recovery_minutes)
        max_hp = int(adv.max_hp or 100)
        if current_hp >= max_hp:
            return '生命值已满，无需治疗'
        return None

    @staticmethod
    def current_treatment_hp(adv, config, now: Optional[datetime] = None) -> int:
        """计算住院期间已完成治疗后的实时 HP。

        医院按整点 HP 结算；提前出院时只获得已用满治疗时长的部分，
        且不超过入院时已购买的治疗点数。
        """
        max_hp = int(adv.max_hp or 100)
        base_hp = max(0, min(int(adv.hp or 0), max_hp))
        extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        target_heal = max(0, int(extra.get('heal_hp') or extra.get('missing_hp') or 0))
        if target_heal <= 0 or not adv.building_assigned_at:
            return base_hp

        minutes_per_hp = max(1, int(config.get('usage_duration_minutes_per_hp') or 1))
        elapsed_seconds = max(
            0.0,
            ((now or _portable_utc_now()) - adv.building_assigned_at).total_seconds(),
        )
        completed_heal = int(elapsed_seconds // (minutes_per_hp * 60))
        return min(max_hp, base_hp + min(target_heal, completed_heal))

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        max_hp = int(adv.max_hp or 100)
        heal_hp = int(extra_params.get('heal_hp') or 0)
        if heal_hp > 0:
            adv.hp = min(max_hp, int(adv.hp or 0) + heal_hp)
        else:
            adv.hp = max_hp
        adv.hp_updated_at = _portable_utc_now()
        return True, None

    def on_recall(self, adv, instance, config):
        """提前出院前先将已完成的治疗量落账，避免退出时丢失恢复量。"""
        now = _portable_utc_now()
        adv.hp = self.current_treatment_hp(adv, config, now=now)
        adv.hp_updated_at = now

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'gold_cost_per_hp': config.get('gold_cost_per_hp', 2),
            'usage_duration_minutes_per_hp': config.get('usage_duration_minutes_per_hp', 2),
            'description': config.get('description', '治疗完成，生命值已恢复'),
        }


class ClinicHandler(BaseBuildingHandler):
    """深渊手术室处理器：按分钟付费的快速回血（酒馆 Lv2 解锁建造）。

    玩家自选驻留分钟数，每分钟扣 gold_cost_per_minute 金币、回 hp_percent_per_minute%
    最大生命值。回血按驻留时间推进，提前出院按已用时长比例结算。
    与医院的差异：医院按整点 HP 预付，本设施按分钟计费、回血量随 max_hp 百分比缩放。
    """
    display_name = '深渊手术室'
    description = '以分钟计费的高级外科设施，按驻留时长持续回复生命值，重伤者可最快重返深渊。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        from flask import current_app
        recovery_minutes = int(current_app.config.get('ADVENTURER_S1_DORM_HP_RECOVERY_MINUTES', 10))
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_hp
        current_hp = get_live_hp(adv, recovery_minutes_per_point=recovery_minutes)
        max_hp = int(adv.max_hp or 100)
        if current_hp >= max_hp:
            return '生命值已满，无需治疗'
        return None

    @staticmethod
    def heal_for_elapsed_minutes(max_hp: int, hp_percent: float, elapsed_minutes: int) -> int:
        """累计百分比回血：T 分钟回 int(T * max_hp * percent / 100)，避免先 round 每分钟再乘分钟数丢小数。"""
        if max_hp <= 0 or hp_percent <= 0 or elapsed_minutes <= 0:
            return 0
        return int(elapsed_minutes * max_hp * hp_percent / 100)

    @staticmethod
    def resolve_hp_percent(extra: dict, config: dict) -> float:
        stored = extra.get('hp_percent_per_minute')
        if stored is not None:
            try:
                pct = float(stored)
                if pct > 0:
                    return pct
            except (TypeError, ValueError):
                pass
        try:
            return float(config.get('hp_percent_per_minute') or 1)
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def current_treatment_hp(adv, config, now: Optional[datetime] = None) -> int:
        """按驻留分钟推进的实时 HP；每分钟回 hp_percent% 最大生命值（累计公式，支持非整数每分钟回量）。"""
        max_hp = int(adv.max_hp or 100)
        base_hp = max(0, min(int(adv.hp or 0), max_hp))
        extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        target_heal = max(0, int(extra.get('heal_hp') or 0))
        if target_heal <= 0 or not adv.building_assigned_at:
            return base_hp
        elapsed_seconds = max(
            0.0,
            ((now or _portable_utc_now()) - adv.building_assigned_at).total_seconds(),
        )
        elapsed_minutes = int(elapsed_seconds // 60)
        if extra.get('hp_percent_per_minute') is not None:
            hp_percent = ClinicHandler.resolve_hp_percent(extra, config)
            completed_heal = ClinicHandler.heal_for_elapsed_minutes(max_hp, hp_percent, elapsed_minutes)
        else:
            # 旧驻留记录仅有整数 hp_per_minute，保持原结算避免回档
            hp_per_minute = max(1, int(extra.get('hp_per_minute') or 1))
            completed_heal = elapsed_minutes * hp_per_minute
        return min(max_hp, base_hp + min(target_heal, completed_heal))

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        """按已驻留时长结算 HP，与 on_recall 一致，避免一次性叠加全额 heal_hp。"""
        now = _portable_utc_now()
        adv.hp = self.current_treatment_hp(adv, config, now=now)
        adv.hp_updated_at = now
        return True, None

    def on_recall(self, adv, instance, config):
        """提前出院前先把已完成治疗量落账，避免退出时丢失恢复量。"""
        now = _portable_utc_now()
        adv.hp = self.current_treatment_hp(adv, config, now=now)
        adv.hp_updated_at = now

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'gold_cost_per_minute': config.get('gold_cost_per_minute', 10),
            'hp_percent_per_minute': config.get('hp_percent_per_minute', 1),
            'description': config.get('description', '治疗完成，生命值已恢复'),
        }


class BlacksmithHandler(BaseBuildingHandler):
    """铁匠铺处理器：AI 驱动的装备强化锻造。

    用户指定两件物品（背包/仓库均可），AI 决定锻造关系和结果。
    成功：消耗投入物品，产出锻造后的新装备（背包满则入仓库）。
    失败：AI 用槽位+冻结名称提议损毁项，服务端据此生成叙事并退还一半金币。
    """
    display_name = '铁匠铺'
    description = '锻造与强化武器防具，由铁匠大师根据材料品质决定强化结果。'
    completion_relationships = ('equipment_items', 'warehouse_items')

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items and not adv.warehouse_items:
            return '该冒险者没有可用于锻造的物品'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        from plugins.adventurer_s1.services.s1_blacksmith_forge_intent import (
            normalize_blacksmith_forge_intent,
        )
        forge_intent, forge_intent_err = normalize_blacksmith_forge_intent(
            extra_params.get('forge_intent'),
        )
        if forge_intent_err:
            return False, TerminalBuildingUsageError(forge_intent_err)
        raw_item_refs = extra_params.get('item_refs') or []
        if not isinstance(raw_item_refs, list) or not raw_item_refs:
            equipment_name = str(extra_params.get('equipment_name') or '').strip()
            raw_materials = extra_params.get('material_names') or []
            legacy_names = [equipment_name] + [str(m).strip() for m in raw_materials if m]
            raw_item_refs = [
                {'source': 'any', 'name': name, 'id': None}
                for name in legacy_names if name
            ]

        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            resolve_item_refs,
            resolve_item_ref,
            validate_items_operation,
        )
        normalized_refs, err = resolve_item_refs(
            adv,
            raw_item_refs,
            min_count=2,
            max_count=2,
            selection_error='铁匠铺需要选择 2 件锻造物品',
        )
        if err:
            return False, TerminalBuildingUsageError(err)
        resolved_items = []
        for ref in normalized_refs:
            item, item_err, _ = resolve_item_ref(adv, ref)
            if item_err:
                return False, TerminalBuildingUsageError(item_err)
            resolved_items.append(item)
        operation_err = validate_items_operation(
            resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='锻造',
        )
        if operation_err:
            return False, TerminalBuildingUsageError(operation_err)

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'item_refs': normalized_refs,
            'forge_intent': forge_intent,
        }
        _bind_async_usage_session('ai_blacksmith', payload, adv, extra_params)
        # 先区分「上次结果还在处理」与真正的提交失败，给前端清晰提示
        if AsyncTaskService.is_task_enqueued_or_running('ai_blacksmith', payload):
            return False, BuildingUsageInProgress('锻造仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_blacksmith', payload):
            return False, '锻造暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class TailorWorkshopHandler(BaseBuildingHandler):
    """绮罗裁坊：AI 驱动的双投入衣装裁制。"""

    display_name = '绮罗裁坊'
    description = (
        '以针线、裁片、编织、层叠与纹饰等精细工艺，改制或制作防具与饰品。'
    )
    completion_relationships = ('equipment_items', 'warehouse_items')

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items and not adv.warehouse_items:
            return '该冒险者没有可用于裁制的物品'
        return None

    def on_complete(
        self,
        adv,
        instance,
        config,
        extra_params: dict = None,
    ) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        from plugins.adventurer_s1.services.s1_tailor_workshop_contract import (
            normalize_tailor_workshop_operation,
        )
        tailor_operation, operation_err = normalize_tailor_workshop_operation(
            extra_params.get('tailor_operation'),
        )
        if operation_err:
            return False, TerminalBuildingUsageError(operation_err)

        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            resolve_item_refs,
            resolve_item_ref,
            validate_items_operation,
        )
        normalized_refs, refs_err = resolve_item_refs(
            adv,
            extra_params.get('item_refs') or [],
            min_count=2,
            max_count=2,
            selection_error='绮罗裁坊需要选择 2 件裁制物品',
        )
        if refs_err:
            return False, TerminalBuildingUsageError(refs_err)
        resolved_items = []
        for ref in normalized_refs:
            item, item_err, _ = resolve_item_ref(adv, ref)
            if item_err:
                return False, TerminalBuildingUsageError(item_err)
            resolved_items.append(item)
        transform_err = validate_items_operation(
            resolved_items,
            ITEM_OPERATION_TRANSFORM_INPUT,
            action_label='裁制',
        )
        if transform_err:
            return False, TerminalBuildingUsageError(transform_err)

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'item_refs': normalized_refs,
            'tailor_operation': tailor_operation,
        }
        _bind_async_usage_session('ai_tailor_workshop', payload, adv, extra_params)
        if AsyncTaskService.is_task_enqueued_or_running('ai_tailor_workshop', payload):
            return False, BuildingUsageInProgress('裁制作业仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_tailor_workshop', payload):
            return False, '裁坊暂时无法开工，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class AlchemyWorkshopHandler(BaseBuildingHandler):
    """炼金工房：AI 驱动的多材料合成。

    用户指定 1~5 件物品（背包/仓库均可），AI 决定合成结果。
    成功：消耗全部投入物品，产出新物品（背包满则入仓库）。
    失败：AI 判断消耗哪些物品；无论如何退还一半金币。
    """
    display_name = '炼金工房'
    description = '将多种材料投入炼金釜，由炼金师根据材料特性尝试合成新物品。'
    completion_relationships = ('equipment_items', 'warehouse_items')

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items and not adv.warehouse_items:
            return '该冒险者没有可用于炼金的物品'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            resolve_item_refs,
            resolve_item_ref,
            validate_items_operation,
        )
        normalized_refs, err = resolve_item_refs(
            adv,
            extra_params.get('item_refs') or [],
            min_count=1,
            max_count=5,
            selection_error='炼金工房需要选择 1~5 件物品',
        )
        if err:
            return False, TerminalBuildingUsageError(err)
        resolved_items = []
        for ref in normalized_refs:
            item, item_err, _ = resolve_item_ref(adv, ref)
            if item_err:
                return False, TerminalBuildingUsageError(item_err)
            resolved_items.append(item)
        operation_err = validate_items_operation(
            resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='炼金',
        )
        if operation_err:
            return False, TerminalBuildingUsageError(operation_err)

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'item_refs': normalized_refs,
        }
        _bind_async_usage_session('ai_alchemy_workshop', payload, adv, extra_params)
        if AsyncTaskService.is_task_enqueued_or_running('ai_alchemy_workshop', payload):
            return False, BuildingUsageInProgress('炼金仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_alchemy_workshop', payload):
            return False, '炼金暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class ItemAssemblyWorkshopHandler(BaseBuildingHandler):
    """构装工坊：AI 驱动的 2~5 件物品排列组合。

    与炼金工房不同，构装工坊不仅看投入物集合，还会把玩家指定的排列关系交给 AI，
    例如从上到下、上下左右、从里到外或环形闭环。
    """
    display_name = '构装工坊'
    description = '将 2~5 件物品按指定排列关系组合构装，由 AI 判断是否形成新物品。'
    completion_relationships = ('equipment_items', 'warehouse_items')

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items and not adv.warehouse_items:
            return '该冒险者没有可用于构装的物品'
        return None

    def _normalize_request(self, adv, extra_params: dict | None) -> tuple[Optional[dict], Optional[str]]:
        extra_params = extra_params or {}
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            expand_item_refs,
            item_refs_total,
            resolve_item_refs,
            resolve_item_ref,
            validate_items_operation,
        )

        raw_refs = extra_params.get('item_refs') or []
        normalized_refs, err = resolve_item_refs(
            adv,
            raw_refs,
            min_count=2,
            max_count=5,
            selection_error='构装工坊需要选择 2~5 件物品（1 件无法构装）',
            preserve_order=True,
        )
        if err:
            return None, TerminalBuildingUsageError(err)
        total = item_refs_total(normalized_refs)

        resolved_items = []
        for ref in normalized_refs:
            item, item_err, _ = resolve_item_ref(adv, ref)
            if item_err:
                return None, TerminalBuildingUsageError(item_err)
            resolved_items.append(item)
        operation_err = validate_items_operation(
            resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='构装',
        )
        if operation_err:
            return None, TerminalBuildingUsageError(operation_err)

        layout_mode = str(extra_params.get('layout_mode') or 'vertical_line').strip()
        if layout_mode not in ITEM_ASSEMBLY_LAYOUT_LABELS:
            return None, TerminalBuildingUsageError('无效的构装排列方式')
        layout_slots = extra_params.get('layout_slots') if isinstance(extra_params.get('layout_slots'), list) else []
        from flask import current_app
        direction_max_length = int(current_app.config.get('ADVENTURER_S1_ASSEMBLY_DIRECTION_MAX_LENGTH', 30))
        desired_direction = str(extra_params.get('desired_direction') or '').strip()
        if len(desired_direction) > direction_max_length:
            return None, TerminalBuildingUsageError(
                f'期望打造方向不能超过 {direction_max_length} 个字',
            )

        expanded_refs = expand_item_refs(normalized_refs)
        if len(expanded_refs) != total:
            return None, TerminalBuildingUsageError('构装投入物数量与实际库存引用不一致')

        submitted_slots: dict[int, dict] = {}
        for raw_slot in layout_slots:
            if not isinstance(raw_slot, dict):
                return None, TerminalBuildingUsageError('构装槽位必须是对象')
            try:
                index = int(raw_slot.get('index'))
            except (TypeError, ValueError):
                return None, TerminalBuildingUsageError('构装槽位编号无效')
            if index < 1 or index > total or index in submitted_slots:
                return None, TerminalBuildingUsageError('构装槽位编号必须完整且不能重复')
            submitted_slots[index] = raw_slot

        legacy_session = bool(extra_params.get('usage_session_id')) and not extra_params.get(
            'input_snapshot_version'
        )
        if submitted_slots and len(submitted_slots) != total:
            return None, TerminalBuildingUsageError('构装槽位必须与投入物逐一对应')

        normalized_slots = []
        for index, ref in enumerate(expanded_refs, 1):
            submitted = submitted_slots.get(index)
            submitted_ref = submitted.get('item_ref') if isinstance(submitted, dict) else None
            if submitted is not None and not legacy_session:
                if not isinstance(submitted_ref, dict):
                    return None, TerminalBuildingUsageError('构装槽位缺少对应物品引用')
                try:
                    submitted_item_id = int(submitted_ref.get('id'))
                except (TypeError, ValueError):
                    return None, TerminalBuildingUsageError('构装槽位物品 id 无效')
                if (
                    str(submitted_ref.get('source') or '') != str(ref.get('source') or '')
                    or submitted_item_id != int(ref.get('id'))
                ):
                    return None, TerminalBuildingUsageError('构装槽位与投入物顺序不一致')
            normalized_slots.append({
                'index': index,
                'position': assembly_layout_position(layout_mode, index, total),
                'item_ref': {
                    'source': str(ref.get('source') or ''),
                    'id': int(ref.get('id')),
                },
            })

        return {
            'item_refs': normalized_refs,
            'layout_mode': layout_mode,
            'layout_slots': normalized_slots,
            'desired_direction': desired_direction,
        }, None

    def prepare_start(self, adv, extra_params: dict | None) -> tuple[Optional[dict], Optional[str]]:
        """校验构装输入并冻结本次会话实际交给 AI 的物品事实。"""
        normalized, err = self._normalize_request(adv, extra_params)
        if err:
            return None, err

        from plugins.adventurer_s1.models import AdventurerS1Item
        from plugins.adventurer_s1.services.s1_item_instance_service import resolve_item_ref

        resolved_rows = []
        names = set()
        for ref in normalized['item_refs']:
            item, item_err, resolved_source = resolve_item_ref(adv, ref)
            if item_err:
                return None, item_err
            resolved_rows.append((ref, item, resolved_source))
            names.add(str(item.name or '').strip())

        catalog_rows = (
            AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
            if names else []
        )
        catalog_desc_by_name = {str(row.name): str(row.description or '') for row in catalog_rows}
        item_snapshots = []
        for ref, item, resolved_source in resolved_rows:
            custom_desc = getattr(item, 'custom_desc', None)
            item_snapshots.append({
                'source': resolved_source,
                'id': int(item.id),
                'name': str(item.name or ''),
                'type': str(getattr(item, 'type', '') or ''),
                'rarity': str(getattr(item, 'rarity', 'common') or 'common'),
                'desc': str(
                    custom_desc
                    or catalog_desc_by_name.get(str(item.name), '')
                    or ''
                ),
                'custom_desc': str(custom_desc) if custom_desc is not None else None,
                'uses_left': getattr(item, 'uses_left', None),
                'evolution_stage': max(0, int(getattr(item, 'evolution_stage', 0) or 0)),
                'damage_stage': getattr(item, 'damage_stage', None),
                'damage_note': getattr(item, 'damage_note', None),
                'input_qty': int(ref.get('quantity') or 1),
            })

        from plugins.adventurer_s1.services.s1_description_ai_service import (
            description_texts_for_ai,
        )
        prompt_descriptions = description_texts_for_ai([
            {
                'description': snapshot.get('desc'),
                'kind': (
                    'companion'
                    if str(snapshot.get('type') or '').lower() == 'companion'
                    else 'item'
                ),
            }
            for snapshot in item_snapshots
        ])
        prompt_snapshots = [
            {**snapshot, 'desc': prompt_descriptions[index]}
            for index, snapshot in enumerate(item_snapshots)
        ]
        input_slots = build_assembly_input_slots(
            prompt_snapshots,
            layout_mode=normalized['layout_mode'],
        )
        snapshot_hash = build_assembly_input_snapshot_hash(
            input_slots=input_slots,
            layout_mode=normalized['layout_mode'],
            desired_direction=normalized['desired_direction'],
        )
        return {
            **normalized,
            'usage_session_id': uuid4().hex,
            'item_snapshots': item_snapshots,
            'input_slots': input_slots,
            'input_snapshot_version': ITEM_ASSEMBLY_INPUT_CONTRACT_VERSION,
            'input_snapshot_hash': snapshot_hash,
        }, None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        raw_extra = extra_params or {}
        normalized, normalize_err = self._normalize_request(adv, raw_extra)
        if normalize_err:
            return False, normalize_err

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            **normalized,
        }
        for internal_key in (
            'usage_session_id', 'item_snapshots', 'input_slots',
            'input_snapshot_version', 'input_snapshot_hash',
        ):
            if raw_extra.get(internal_key):
                payload[internal_key] = raw_extra[internal_key]
        _bind_async_usage_session('ai_item_assembly_workshop', payload, adv, raw_extra)
        if AsyncTaskService.is_task_enqueued_or_running('ai_item_assembly_workshop', payload):
            return False, BuildingUsageInProgress('构装仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_item_assembly_workshop', payload):
            return False, '构装暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class EmberKitchenHandler(BaseBuildingHandler):
    """余烬灶坊：冻结 1~5 份食材与建筑自带炊具，异步生成并审核餐食。"""
    display_name = '余烬灶坊'
    description = '选择 1~5 份食材和灶坊自带炊具，尝试制作便携消耗品。'
    completion_relationships = ('equipment_items', 'warehouse_items')

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items and not adv.warehouse_items:
            return '该冒险者没有可用于烹饪的食材'
        return None

    def _normalize_request(
        self,
        adv,
        extra_params: dict | None,
    ) -> tuple[Optional[dict], Optional[str]]:
        extra_params = extra_params or {}
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            resolve_item_ref,
            resolve_item_refs,
            validate_items_operation,
        )

        normalized_refs, err = resolve_item_refs(
            adv,
            extra_params.get('item_refs') or [],
            min_count=1,
            max_count=5,
            selection_error='余烬灶坊需要选择 1~5 份食材',
        )
        if err:
            return None, TerminalBuildingUsageError(err)

        resolved_items = []
        for ref in normalized_refs:
            item, item_err, _ = resolve_item_ref(adv, ref)
            if item_err:
                return None, TerminalBuildingUsageError(item_err)
            resolved_items.append(item)
        operation_err = validate_items_operation(
            resolved_items,
            ITEM_OPERATION_TRANSFORM_INPUT,
            action_label='烹饪',
        )
        if operation_err:
            return None, TerminalBuildingUsageError(operation_err)

        cookware_code = str(extra_params.get('cookware_code') or '').strip()
        if cookware_code not in EMBER_KITCHEN_COOKWARE:
            return None, TerminalBuildingUsageError('请选择余烬灶坊提供的炊具')
        return {
            'item_refs': normalized_refs,
            'cookware_code': cookware_code,
        }, None

    def prepare_start(
        self,
        adv,
        extra_params: dict | None,
    ) -> tuple[Optional[dict], Optional[str]]:
        """冻结本次烹饪真正交给生成与结算链路的食材事实。"""
        normalized, err = self._normalize_request(adv, extra_params)
        if err:
            return None, err

        from plugins.adventurer_s1.models import AdventurerS1Item
        from plugins.adventurer_s1.services.s1_description_ai_service import (
            description_texts_for_ai,
        )
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            resolve_item_ref,
        )

        resolved_rows = []
        names = set()
        for ref in normalized['item_refs']:
            item, item_err, resolved_source = resolve_item_ref(adv, ref)
            if item_err:
                return None, TerminalBuildingUsageError(item_err)
            resolved_rows.append((ref, item, resolved_source))
            names.add(str(item.name or '').strip())

        catalog_rows = (
            AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
            if names else []
        )
        catalog_desc_by_name = {
            str(row.name): str(row.description or '') for row in catalog_rows
        }
        item_snapshots = []
        for ref, item, resolved_source in resolved_rows:
            custom_desc = getattr(item, 'custom_desc', None)
            item_snapshots.append({
                'source': resolved_source,
                'id': int(item.id),
                'name': str(item.name or ''),
                'type': str(getattr(item, 'type', '') or ''),
                'rarity': str(getattr(item, 'rarity', 'common') or 'common'),
                'desc': str(
                    custom_desc
                    or catalog_desc_by_name.get(str(item.name), '')
                    or ''
                ),
                'custom_desc': str(custom_desc) if custom_desc is not None else None,
                'uses_left': getattr(item, 'uses_left', None),
                'evolution_stage': max(
                    0, int(getattr(item, 'evolution_stage', 0) or 0),
                ),
                'recovery_minutes': max(
                    0, int(getattr(item, 'recovery_minutes', 0) or 0),
                ),
                'recovery_started_at': (
                    getattr(item, 'recovery_started_at', None).isoformat()
                    if getattr(item, 'recovery_started_at', None) else None
                ),
                'recovery_ends_at': (
                    getattr(item, 'recovery_ends_at', None).isoformat()
                    if getattr(item, 'recovery_ends_at', None) else None
                ),
                'damage_stage': getattr(item, 'damage_stage', None),
                'damage_note': getattr(item, 'damage_note', None),
                'quantity_at_start': max(
                    1, int(getattr(item, 'quantity', 1) or 1),
                ),
                'is_player_locked': bool(
                    getattr(item, 'is_player_locked', False),
                ),
                'created_at': (
                    getattr(item, 'created_at', None).isoformat()
                    if getattr(item, 'created_at', None) else None
                ),
                'updated_at': (
                    getattr(item, 'updated_at', None).isoformat()
                    if getattr(item, 'updated_at', None) else None
                ),
                'input_qty': int(ref.get('quantity') or 1),
            })

        prompt_descriptions = description_texts_for_ai([{
            'description': snapshot.get('desc'),
            'kind': (
                'companion'
                if str(snapshot.get('type') or '').lower() == 'companion'
                else 'item'
            ),
        } for snapshot in item_snapshots])
        prompt_snapshots = [
            {**snapshot, 'desc': prompt_descriptions[index]}
            for index, snapshot in enumerate(item_snapshots)
        ]
        input_slots = build_ember_kitchen_input_slots(prompt_snapshots)
        snapshot_hash = build_ember_kitchen_input_snapshot_hash(
            input_slots=input_slots,
            cookware_code=normalized['cookware_code'],
        )
        return {
            **normalized,
            'usage_session_id': uuid4().hex,
            'item_snapshots': item_snapshots,
            'input_slots': input_slots,
            'input_snapshot_version': EMBER_KITCHEN_INPUT_CONTRACT_VERSION,
            'input_snapshot_hash': snapshot_hash,
        }, None

    def on_complete(
        self,
        adv,
        instance,
        config,
        extra_params: dict = None,
    ) -> Tuple[bool, Optional[str]]:
        raw_extra = extra_params or {}
        normalized, normalize_err = self._normalize_request(adv, raw_extra)
        if normalize_err:
            return False, normalize_err

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            **normalized,
        }
        for internal_key in (
            'usage_session_id',
            'item_snapshots',
            'input_slots',
            'input_snapshot_version',
            'input_snapshot_hash',
        ):
            if raw_extra.get(internal_key):
                payload[internal_key] = raw_extra[internal_key]
        _bind_async_usage_session('ai_ember_kitchen', payload, adv, raw_extra)
        if AsyncTaskService.is_task_enqueued_or_running('ai_ember_kitchen', payload):
            return False, BuildingUsageInProgress('炉火仍旺，请等待餐食出锅')
        if not AsyncTaskService.submit_task('ai_ember_kitchen', payload):
            return False, '灶坊暂时无法开火，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


def get_training_cap(level: int) -> int:
    """训练总上限 = BASE + 等级 × PER_LEVEL（三个训练场共用一池）。"""
    from flask import current_app
    base = int(current_app.config.get('ADVENTURER_S1_TRAINING_CAP_BASE', 4))
    per_level = int(current_app.config.get('ADVENTURER_S1_TRAINING_CAP_PER_LEVEL', 2))
    return base + max(0, int(level or 0)) * per_level


class TrainingHandler(BaseBuildingHandler):
    """训练场处理器：累计训练次数受总上限约束（上限随等级成长），属性结算走默认 settlement。"""
    display_name = '训练场'
    description = '属性训练设施，累计训练次数受总上限约束（升级解锁更多次数）。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        cap = get_training_cap(adv.level)
        used = int(getattr(adv, 'training_count', 0) or 0)
        if used >= cap:
            return f'训练潜能已耗尽（{used}/{cap}），提升等级可解锁更多训练次数'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        # 上限兜底（防绕过 start 直接 complete）：满了就不结算也不计数
        cap = get_training_cap(adv.level)
        used = int(getattr(adv, 'training_count', 0) or 0)
        if used >= cap:
            return True, None  # 跳过默认结算，不发属性
        adv.training_count = used + 1
        return False, None  # fallback 默认 settlement 发放属性 +1


class SeatManagedBuildingHandler(BaseBuildingHandler):
    """专用座位建筑的通用 start/complete 边界。"""

    seat_label = '座位'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return (
            f'「{self.display_name}」使用专用座位流程，'
            f'请在场所内选择{self.seat_label}'
        )

    def on_complete(
        self,
        adv,
        instance,
        config,
        extra_params: dict = None,
    ) -> Tuple[bool, Optional[str]]:
        # 历史异常占用不得回落到通用 settlement 发放建筑奖励。
        return True, None


class TavernHallHandler(SeatManagedBuildingHandler):
    """酒馆大堂处理器：座位系统独立管理，不走 start/complete 流程。

    入座/离座由 AdventurerS1TavernSeatService 直接管理，
    此 handler 仅用于标识建筑类型，不实现业务逻辑。
    """
    display_name = '酒馆大堂'
    description = '冒险者聚集饮酒的地方，入座后可点酒休息，缓慢恢复精力。'


class TeaHouseHandler(SeatManagedBuildingHandler):
    """深渊茶舍处理器：座位系统独立管理，不走 start/complete 流程。

    入座/离座由 AdventurerS1TeaHouseSeatService 直接管理。
    入座扣 30 金币入场费，精力按 2/分钟恢复，最多待 6 小时。
    """
    display_name = '深渊茶舍'
    description = '安静的喝茶场所，入座后精力恢复速率提升至 2/分钟，适合快速恢复状态。'
    seat_label = '茶座'


class HotSpringHandler(SeatManagedBuildingHandler):
    """深渊温泉处理器：座位系统独立管理，不走 start/complete 流程。

    入座/离座由 AdventurerS1HotSpringSeatService 直接管理。
    入座扣 200 金币入场费，生命值 1 点 / 2 分钟、精力 1 点 / 1 分钟恢复，泡满或最多待 6 小时后自动离场。
    """
    display_name = '深渊温泉'
    description = '热气蒸腾的露天温泉，入座后生命值每两分钟、精力每一分钟恢复一点，泡满或最多六小时后自动起身。'
    seat_label = '汤池座位'


class FallenMemorialHandler(BaseBuildingHandler):
    """英灵碑林：展示阵亡冒险者墓碑，不走 start/complete 流程。"""
    display_name = '英灵碑林'
    description = '存放阵亡冒险者的墓碑，刻录其最后一段叙事。'


class FlowerShopHandler(BaseBuildingHandler):
    """鲜花店：只负责解锁英灵碑林内的付费送花入口。"""
    display_name = '鲜花店'
    description = '提供仅用于英灵碑林纪念展示的鲜花；请在墓地选定墓碑后送出。'



def calculate_pet_evolution_gold_cost(base_cost: float, evolution_stage: int) -> float:
    """计算下一阶嬗变费用：1-3 阶原价，第 4 阶起每阶在上一阶基础上翻倍。"""
    stage = max(0, int(evolution_stage or 0))
    multiplier = 1 if stage < 3 else 2 ** (stage - 2)
    return round(max(0.0, float(base_cost or 0)) * multiplier, 2)


class PetEvolutionHandler(BaseBuildingHandler):
    """嬗变祭坛：AI 驱动任意伙伴嬗变。

    冒险者携带伙伴进入祭坛，由祭坛守护者评估并尝试引导嬗变。
    companion 类型成功率较高，其他类型大概率失败。
    成功：伙伴的 name/custom_desc/rarity 原地更新，evolution_stage +1。
    失败：退还一半金币，伙伴原样保留且不消耗；辅助素材不返还。
    """
    display_name = '嬗变祭坛'
    description = '古老的嬗变圣坛，守护者沉眠于此，能引导同伴经历嬗变、突破为更高阶的存在。'
    completion_relationships = ('equipment_items',)

    def can_start(self, adv, instance, config) -> Optional[str]:
        if not adv.equipment_items:
            return '该冒险者没有可嬗变的伙伴'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        companion_id = extra_params.get('companion_id')
        if companion_id is None:
            return False, TerminalBuildingUsageError('请选择要嬗变的伙伴')

        # 校验伙伴存在且属于该冒险者
        companion = None
        for eq in adv.equipment_items:
            if eq.id == int(companion_id):
                companion = eq
                break
        if not companion:
            return False, TerminalBuildingUsageError('未找到指定的伙伴')
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_TRANSFORM_INPUT,
            validate_item_operation,
        )
        operation_err = validate_item_operation(
            companion, ITEM_OPERATION_TRANSFORM_INPUT, action_label='嬗变',
        )
        if operation_err:
            return False, TerminalBuildingUsageError(operation_err)
        stored_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        auxiliary_material = stored_extra.get('auxiliary_material')
        charged_gold_cost = float(stored_extra.get('gold_cost') or config.get('gold_cost') or 0)
        try:
            evolution_stage_at_start = max(0, int(stored_extra.get('evolution_stage')))
        except (TypeError, ValueError):
            evolution_stage_at_start = max(0, int(companion.evolution_stage or 0))

        from services.async_task_service import AsyncTaskService
        payload = {
            'adventurer_id': adv.id,
            'building_instance_id': instance.id,
            'companion_id': companion.id,
            'charged_gold_cost': charged_gold_cost,
            'auxiliary_material': auxiliary_material,
            'evolution_stage_at_start': evolution_stage_at_start,
        }
        _bind_async_usage_session('ai_pet_evolution', payload, adv, stored_extra)
        if AsyncTaskService.is_task_enqueued_or_running('ai_pet_evolution', payload):
            return False, BuildingUsageInProgress('嬗变仍在进行，请等待结果通知')
        if not AsyncTaskService.submit_task('ai_pet_evolution', payload):
            return False, '嬗变暂时无法开始，请稍后重试'
        return True, None

    def is_async(self) -> bool:
        return True


class InsuranceShopHandler(BaseBuildingHandler):
    """保险局处理器：购买探险保险，结算后根据险种自动赔付。"""
    display_name = '保险局'
    description = '购买受伤保险，探险结算后根据受伤程度自动赔付（最高 500 金币）。无需派遣冒险者。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return '保险局无需派遣冒险者，请直接在保险界面购买'


class DiningHallHandler(BaseBuildingHandler):
    """深渊食堂：选餐付金币，用餐完成后获得下次探险一次性临时加成。"""
    display_name = '深渊食堂'
    description = '花金币点一份热饭，下一趟探险获得小幅临时属性加成。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        extra_params = extra_params or {}
        food_code = str(extra_params.get('food_code') or '').strip()
        if not food_code:
            stored = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
            food_code = str(stored.get('food_code') or '').strip()
        food, err = S1DiningService.resolve_food(food_code)
        if err:
            return False, err
        S1DiningService.apply_buff_to_adventurer(adv, food)

        # Lv2 升级效果：所有菜品额外恢复精力
        level = int(getattr(instance, 'upgrade_level', 1) or 1)
        if level > 1 and instance.template and instance.template.upgrade_config:
            effects = (instance.template.upgrade_config.get('handler_effects') or {})
            stamina_restore = int(effects.get('stamina_restore', 0) or 0)
            if stamina_restore > 0:
                from flask import current_app
                max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
                adv.stamina = min(max_stamina, int((adv.stamina or 0) + stamina_restore))

        return True, None

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'description': config.get('description', '用餐完成，获得下次探险加成'),
            'usage_duration_minutes': config.get('usage_duration_minutes', 10),
            'cooldown_hours': config.get('cooldown_hours', 4),
        }


class ClueBureauHandler(BaseBuildingHandler):
    """线索追查处：归来随机掉落线索；小队凑齐四条触发隐藏 Boss，讨伐成功结算时消耗线索并兑现金币。"""
    display_name = '线索追查处'
    description = (
        '出门追查隐藏 Boss 线索：按出门时长消耗精力，有机会随机获得线索。'
        '小队凑齐四条后可在对应副本触发隐藏 Boss；讨伐成功结算时消耗对应线索，'
        '每条兑 300 金币（同一 Boss 重复线索合计上限 1200 金，按消耗条数平分）。'
    )

    def can_start(self, adv, instance, config) -> Optional[str]:
        from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService
        S1ClueBureauService.ensure_trace_pool_bootstrapped()
        if not S1ClueBureauService._load_pool():
            return '追查池为空，请联系管理员补充可追查 Boss'
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService

        stored = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        params = extra_params if isinstance(extra_params, dict) else {}
        try:
            search_hours = int(params.get('search_hours') or stored.get('search_hours') or 0)
        except (TypeError, ValueError):
            search_hours = 0

        granted, messages, err = S1ClueBureauService.grant_clues_for_search_hours(
            adv, search_hours, config,
        )
        if err:
            logger.warning('线索追查处发放失败 adv=%s inst=%s: %s', adv.id, instance.id, err)
            return True, None
        adv.building_usage_extra = {
            **stored,
            'last_clue_labels': granted,
            'last_clue_messages': messages,
            'last_clue_message': '；'.join(messages) if messages else '',
        }
        return True, None

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'stamina_cost_per_hour': config.get('stamina_cost_per_hour', 20),
            'max_search_hours': config.get('max_search_hours', 6),
            'description': config.get('description', '线索追查归来'),
        }


class ScavengeHandler(BaseBuildingHandler):
    """深渊拾荒队：派冒险者出门搜寻，按出门时长（档位）消耗金币与精力，
    归来（到期结算）时从不同稀有度物品池中随机搜得一批战利品。

    产出来源（后端 roll / AI 生成）由 settlement_config.result_source 切换，
    具体逻辑在 S1ScavengeService。同步 handler：到期由通用 complete 流程结算并释放。
    """
    display_name = '深渊拾荒队'
    description = '派冒险者出门搜寻遗物，按出门时长消耗金币与精力，归来时随机带回一批不同稀有度的战利品。'

    def can_start(self, adv, instance, config) -> Optional[str]:
        return None

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        from plugins.adventurer_s1.services.s1_scavenge_service import S1ScavengeService
        stored = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        params = extra_params if isinstance(extra_params, dict) else {}
        try:
            search_hours = int(params.get('search_hours') or stored.get('search_hours') or 1)
        except (TypeError, ValueError):
            search_hours = 1

        loot, err = S1ScavengeService.generate_loot(adv, instance, search_hours, config)
        if err:
            # 产出失败不阻断释放（避免冒险者卡在 building 态），记日志即可
            logger.warning('拾荒产出失败 adv=%s inst=%s: %s', adv.id, instance.id, err)
            return True, None
        S1ScavengeService.grant_loot(adv, loot)
        return True, None

    def get_reward_preview(self, instance, config) -> dict:
        return {
            'gold_cost_per_hour': config.get('gold_cost_per_hour', 0),
            'usage_stamina_cost': config.get('usage_stamina_cost', 0),
            'max_search_hours': config.get('max_search_hours', 8),
            'description': config.get('description', '拾荒归来'),
        }


class PigstyCleaningHandler(BaseBuildingHandler):
    """后院猪圈：结算普通打工报酬，并以会话稳定的概率发放普通伙伴。"""

    display_name = '后院猪圈'
    description = '添满饲槽、补足清水并照看猪群；喂猪时有小概率得到一只亲人的小猪。'

    @staticmethod
    def _piglet_reward_roll(adv, instance) -> float:
        """同一次工作重试保持同一结果，且不让客户端根据公开时间预判。"""
        from flask import current_app

        assigned_at = adv.building_assigned_at
        if assigned_at is None:
            return 1.0
        secret_key = current_app.config.get('SECRET_KEY')
        if not secret_key:
            raise RuntimeError('服务器抽奖密钥未配置')
        if isinstance(secret_key, str):
            secret_key = secret_key.encode('utf-8')
        usage_key = assigned_at.isoformat(timespec='microseconds')
        roll_key = f'pigsty_cleaning:{adv.id}:{instance.id}:{usage_key}'.encode('utf-8')
        digest = hmac.new(secret_key, roll_key, hashlib.sha256).digest()
        return int.from_bytes(digest[:8], 'big') / float(1 << 64)

    def on_complete(self, adv, instance, config, extra_params: dict = None) -> Tuple[bool, Optional[str]]:
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import (
            BuildingSettlementService,
        )

        settled, settle_err = BuildingSettlementService.settle(
            adv,
            instance,
            source_type='building_usage_complete',
        )
        if not settled:
            return True, settle_err

        try:
            raw_chance = float(config.get('piglet_reward_chance') or 0.0)
        except (TypeError, ValueError):
            raw_chance = 0.0
        chance = max(0.0, min(1.0, raw_chance)) if math.isfinite(raw_chance) else 0.0
        if chance <= 0.0 or self._piglet_reward_roll(adv, instance) >= chance:
            return True, None

        item_name = str(config.get('piglet_reward_item') or '').strip()
        if not item_name:
            return True, '猪圈小猪奖励配置缺失'

        from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item

        _, grant_err = grant_catalog_item(
            adv,
            item_name,
            source_type='pigsty_cleaning_reward',
            description=f'在后院猪圈喂猪时获得「{item_name}」',
            related_id=str(instance.id),
            related_type='building_instance',
        )
        if grant_err:
            return True, grant_err
        return True, None


# ── 使用岗位用词（并发上限提示 / 卡片展示） ───────────────────────────────────

_USAGE_SLOT_LABEL_BY_HANDLER: dict[str, str] = {
    'hospital': '病床',
    'clinic': '病床',
    'sanatorium': '床位',
    'dining_hall': '用餐位',
    'gambling_hall': '赌桌',
    'skill_school': '传艺席',
    'blacksmith': '锻造位',
    'tailor_workshop': '裁制位',
    'alchemy_workshop': '炼金位',
    'healing_house': '治疗位',
    'chapel': '忏悔位',
    'pet_evolution': '嬗变位',
    'item_assembly_workshop': '构装位',
    'ember_kitchen': '灶位',
    'tea_house': '茶座',
    'hot_spring': '汤池座',
    'insurance_shop': '柜台',
    'scavenge': '搜寻位',
    'clue_bureau': '追查位',
    'world_intel': '情报位',
}

_USAGE_SLOT_LABEL_BY_CATEGORY: dict[str, str] = {
    'work': '工位',
    'gathering': '作业位',
    'training': '训练位',
    'leisure': '服务位',
}


def get_usage_slot_label(
    handler_key: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """返回建筑「使用并发位」的中文量词（工位 / 病床 / 用餐位等）。"""
    if handler_key and handler_key in _USAGE_SLOT_LABEL_BY_HANDLER:
        return _USAGE_SLOT_LABEL_BY_HANDLER[handler_key]
    if category and category in _USAGE_SLOT_LABEL_BY_CATEGORY:
        return _USAGE_SLOT_LABEL_BY_CATEGORY[category]
    return '工位'


def format_usage_slots_full_message(
    current: int,
    max_slots: int,
    handler_key: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    label = get_usage_slot_label(handler_key, category)
    return f'{label}已满（{current}/{max_slots}）'


# ── 注册表 ──────────────────────────────────────────────────────────────────

BUILDING_HANDLERS: dict[str, BaseBuildingHandler] = {
    'secret_auction': SecretAuctionHandler(),
    'hidden_dungeon_exchange': HiddenDungeonExchangeHandler(),
    'herbalist_shop': HerbalistShopHandler(),
    'armory_shop': ArmoryShopHandler(),
    'forge_supply_shop': ForgeSupplyShopHandler(),
    'limited_shop': LimitedShopHandler(),
    'buyback_shop': BuybackShopHandler(),
    'gambling_hall': GamblingHallHandler(),
    'lucky_wheel': LuckyWheelHandler(),
    'skill_school': SkillSchoolHandler(),
    'task_board': TaskBoardHandler(),
    'campaign_hall': CampaignHallHandler(),
    'dormitory': DormitoryHandler(),
    'training': TrainingHandler(),
    'healing_house': HealingHouseHandler(),
    'chapel': ChapelHandler(),
    'hospital': HospitalHandler(),
    'clinic': ClinicHandler(),
    'sanatorium': SanatoriumHandler(),
    'tavern_hall': TavernHallHandler(),
    'tea_house': TeaHouseHandler(),
    'hot_spring': HotSpringHandler(),
    'fallen_memorial': FallenMemorialHandler(),
    'flower_shop': FlowerShopHandler(),

    'blacksmith': BlacksmithHandler(),
    'tailor_workshop': TailorWorkshopHandler(),
    'alchemy_workshop': AlchemyWorkshopHandler(),
    'item_assembly_workshop': ItemAssemblyWorkshopHandler(),
    'ember_kitchen': EmberKitchenHandler(),
    'coming_soon': ComingSoonHandler(),
    'transcendent_court_preview': TranscendentAssessmentPreviewHandler(),
    'contribution_monument': ContributionMonumentHandler(),
    'dining_hall': DiningHallHandler(),
    'pet_evolution': PetEvolutionHandler(),
    'insurance_shop': InsuranceShopHandler(),
    'scavenge': ScavengeHandler(),
    'pigsty_cleaning': PigstyCleaningHandler(),
    'clue_bureau': ClueBureauHandler(),
    'world_intel': WorldIntelHandler(),
}


def get_handler(key: Optional[str]) -> Optional[BaseBuildingHandler]:
    """根据 handler_key 获取处理器实例。None 或未注册的 key 返回 None。"""
    if not key:
        return None
    return BUILDING_HANDLERS.get(key)


def get_all_handler_info() -> list[dict]:
    """返回所有已注册 handler 的信息，供 GM 前端选择用。"""
    return [
        {'key': key, 'name': h.display_name, 'description': h.description}
        for key, h in BUILDING_HANDLERS.items()
    ]
