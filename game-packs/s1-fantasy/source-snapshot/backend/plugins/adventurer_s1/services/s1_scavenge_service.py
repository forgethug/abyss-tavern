"""深渊酒馆 S1「深渊拾荒队」搜寻建筑服务。

冒险者出门搜寻：消耗精力 + 金币 + 时间（按小时档位，默认最多 8 小时），
到期归来时从不同稀有度的物品池中随机搜得一批战利品。

产出来源抽象为可切换的 provider（settlement_config.result_source）：
  - 'backend'：后端按稀有度概率本地 roll（默认，无 AI 成本）—— 本文件已实现
  - 'ai'：预留接口，由 AI 编写战利品 —— 见 _ai_generate_loot（待接入，
          参考 s1_building_ai_service.call_blacksmith_ai 的异步模式）

settlement_config 关键字段（在 seed.BUILDING_TEMPLATES 配置）：
  - gold_cost_per_hour：每小时金币消耗
  - usage_stamina_cost：固定精力消耗
  - usage_slots：并发搜寻位
  - max_search_hours：档位上限（默认 8）
  - result_source：'backend'|'ai'（默认 backend）

注：本文件中的稀有度权重、战利品池、产出件数曲线均为初版占位，
    待玩法平衡后调整；战利品池内容亦可整体替换为 AI 生成。
"""
import logging
import math
import random
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

MIN_SEARCH_HOURS = 1
DEFAULT_MAX_SEARCH_HOURS = 8

# 常规战利品只 roll common / rare（垃圾填充）；真正值钱的 epic 门票走独立概率通道。
_RARITY_TIERS = ('common', 'rare')

# 稀有度基础权重（相对值，random.choices 按相对权重抽样）。
_BASE_RARITY_WEIGHTS = {
    'common': 76,
    'rare': 24,
}

# 出门时长对稀有的加权：越久略增 rare 占比，common 不变。
_RARITY_HOURS_BONUS = {
    'common': 0.0,
    'rare': 0.8,
}

# 特殊副本门票掉落（占位，不下发前端）：传说 1% 优先，否则史诗 8%，单次出门最多一张。
_EPIC_TICKET_DROP_CHANCE = 0.08
_LEGENDARY_TICKET_DROP_CHANCE = 0.01

# 内置战利品池（按稀有度分组）。常规产出以可收购的垃圾为主；特殊副本门票不在此池。
# 每项结构：{name, type, desc[, uses_left]}；type 走图鉴自由字符串（consumable/misc…）。
_LOOT_POOL = {
    'common': [
        {'name': '破损的口粮', 'type': 'consumable', 'desc': '搜寻途中找到的干粮，勉强还能入口。'},
        {'name': '生锈的零件', 'type': 'misc', 'desc': '不知从何处拆下的金属零件，收购商行会象征性收下。'},
        {'name': '杂草药束', 'type': 'consumable', 'desc': '随手采来的草药，效用平平。'},
        {'name': '浸水的账本残页', 'type': 'misc', 'desc': '字迹糊成一团，只剩边角还能辨认。'},
        {'name': '裂开的陶罐底', 'type': 'misc', 'desc': '陶罐早已碎尽，只剩底部一块尚可把玩。'},
        {'name': '弯折的铜钉', 'type': 'misc', 'desc': '从朽木里拔出的铜钉，弯得不成样子。'},
        {'name': '霉斑布条', 'type': 'misc', 'desc': '带着潮气与霉味的破布，只能当填充物。'},
        {'name': '空药瓶', 'type': 'misc', 'desc': '瓶口还留着药渣气味，里面早就被倒空了。'},
        {'name': '断柄小勺', 'type': 'misc', 'desc': '勺柄断在半路，勺面倒是擦得发亮。'},
        {'name': '油污手套', 'type': 'misc', 'desc': '一层洗不掉的油垢，戴着嫌脏，扔了又可惜。'},
        {'name': '褪色标签', 'type': 'misc', 'desc': '原本写着什么已不可读，只剩浆糊与折痕。'},
        {'name': '松动的齿轮', 'type': 'misc', 'desc': '齿牙缺了几处，转起来咔咔作响。'},
        {'name': '碎裂的玻璃片', 'type': 'misc', 'desc': '边缘还算圆润，小心别划破手指。'},
    ],
    'rare': [
        {'name': '风化的护符', 'type': 'accessory', 'desc': '刻着模糊纹路的护符，力量几乎散尽，只剩一点收藏价值。'},
        {'name': '完好的工具箱', 'type': 'misc', 'desc': '保存尚好的旧工具，比多数废墟货像样些。'},
        {'name': '尚能使用的绷带', 'type': 'consumable', 'desc': '干燥洁净，勉强还能包扎小伤。'},
        {'name': '封装完好的药粉', 'type': 'consumable', 'desc': '蜡封未破，药粉尚干，效用却谈不上出色。'},
        {'name': '砥砺过的短钉', 'type': 'misc', 'desc': '有人认真磨过刃口，仍只是寻常铁钉。'},
        {'name': '擦亮的铜扣', 'type': 'misc', 'desc': '扣面被擦得发亮，穿绳处却磨得发薄。'},
        {'name': '未开封的燧石', 'type': 'misc', 'desc': '小袋燧石干燥完好，生火还算可靠。'},
        {'name': '保存完好的锁片', 'type': 'misc', 'desc': '锁片完整无缺，可惜没有配套的锁身。'},
        {'name': '卷边的地图残片', 'type': 'misc', 'desc': '只标注了一小段岔路，难说还有没有用。'},
    ],
}


def _draw_count(hours: int) -> int:
    """出门时长 → 抽取次数。占位曲线：每满 2 小时多搜一件（1-2h:1 … 7-8h:4）。"""
    return max(1, math.ceil(hours / 2))


def _roll_rarity(hours: int) -> str:
    """按基础权重 + 时长加权随机一个稀有度。"""
    tiers, weights = [], []
    for tier in _RARITY_TIERS:
        w = _BASE_RARITY_WEIGHTS.get(tier, 0) + _RARITY_HOURS_BONUS.get(tier, 0.0) * hours
        if w > 0:
            tiers.append(tier)
            weights.append(w)
    if not tiers:
        return 'common'
    return random.choices(tiers, weights=weights, k=1)[0]


class S1ScavengeService:
    """深渊拾荒队：出门档位解析 + 战利品产出（后端 / AI 可切换）。"""

    # ── 出门档位 ──────────────────────────────────────────────────────────────

    @staticmethod
    def resolve_dispatch(
        config: dict,
        extra_params: Optional[dict],
    ) -> Tuple[Optional[Tuple[float, int, int]], Optional[str]]:
        """解析出门档位 → ((gold_cost, duration_minutes, search_hours), err)。按小时计费。"""
        gold_per_hour = int((config or {}).get('gold_cost_per_hour') or 0)
        max_hours = int((config or {}).get('max_search_hours') or DEFAULT_MAX_SEARCH_HOURS)
        if max_hours < MIN_SEARCH_HOURS:
            max_hours = DEFAULT_MAX_SEARCH_HOURS

        raw = (extra_params or {}).get('search_hours')
        if raw is None:
            return None, '请选择出门搜寻时长'
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            return None, '出门时长无效'
        if hours < MIN_SEARCH_HOURS:
            return None, f'出门时长至少 {MIN_SEARCH_HOURS} 小时'
        if hours > max_hours:
            return None, f'单次出门最多 {max_hours} 小时'

        gold_cost = hours * gold_per_hour
        duration_minutes = hours * 60
        return (gold_cost, duration_minutes, hours), None

    # ── 战利品产出（统一入口，按 result_source 分发） ───────────────────────────

    @staticmethod
    def generate_loot(
        adv,
        instance,
        search_hours: int,
        config: dict,
    ) -> Tuple[list[dict], Optional[str]]:
        """生成本次搜寻战利品列表。

        返回的每项结构统一：{name, type, rarity, quantity, desc[, uses_left]}，
        供 grant_loot 落地。AI 产出失败时回退后端生成，保证到期结算不空转。
        """
        hours = max(MIN_SEARCH_HOURS, int(search_hours or MIN_SEARCH_HOURS))
        source = str((config or {}).get('result_source') or 'backend').strip().lower()

        loot: Optional[list[dict]] = None
        if source == 'ai':
            ai_loot, err = S1ScavengeService._ai_generate_loot(adv, instance, hours, config)
            if err:
                logger.warning('拾荒 AI 产出未就绪，回退后端生成: %s', err)
            elif ai_loot is not None:
                loot = ai_loot
        if loot is None:
            loot = S1ScavengeService._backend_roll_loot(hours, config)

        # 占位：小概率额外掉落特殊副本门票（与稀有度战利品并列）。
        S1ScavengeService._maybe_append_special_dungeon_ticket(loot)
        return loot, None

    @staticmethod
    def _maybe_append_special_dungeon_ticket(loot: list[dict]) -> None:
        """按稀有度分池追加一张特殊副本门票（概率占位，不下发前端）。"""
        from plugins.adventurer_s1.services.adventurer_s1_config_service import (
            get_s1_special_dungeons_by_ticket_rarity,
        )

        roll = random.random()
        if roll < _LEGENDARY_TICKET_DROP_CHANCE:
            pool = get_s1_special_dungeons_by_ticket_rarity('legendary')
        elif roll < _LEGENDARY_TICKET_DROP_CHANCE + _EPIC_TICKET_DROP_CHANCE:
            pool = get_s1_special_dungeons_by_ticket_rarity('epic')
        else:
            return
        if not pool:
            return
        dungeon = random.choice(pool)
        loot.append({
            'name': dungeon['ticket_item'],
            'type': 'ticket',
            'rarity': str(dungeon.get('ticket_rarity') or 'epic'),
            'quantity': 1,
            'uses_left': None,
            'desc': dungeon.get('flavor', ''),
        })

    # ── 后端生成（已实现） ──────────────────────────────────────────────────────

    @staticmethod
    def _backend_roll_loot(search_hours: int, config: dict) -> list[dict]:
        """后端按稀有度概率 roll 一批战利品。"""
        loot: list[dict] = []
        for _ in range(_draw_count(search_hours)):
            rarity = _roll_rarity(search_hours)
            pool = _LOOT_POOL.get(rarity) or _LOOT_POOL['common']
            tpl = dict(random.choice(pool))
            loot.append({
                'name': tpl['name'],
                'type': tpl.get('type', 'misc'),
                'rarity': rarity,
                'quantity': 1,
                'uses_left': tpl.get('uses_left'),
                'desc': tpl.get('desc', ''),
            })
        return loot

    # ── AI 生成（预留接口） ────────────────────────────────────────────────────

    @staticmethod
    def _ai_generate_loot(
        adv,
        instance,
        search_hours: int,
        config: dict,
    ) -> Tuple[Optional[list[dict]], Optional[str]]:
        """【预留接口】由 AI 生成搜寻战利品。

        后续接入时（参考 s1_building_ai_service.call_blacksmith_ai 的异步模式 +
        AsyncTaskService worker 分支），在此调用 AI 编写战利品并返回；返回的每项
        需含 name/type/rarity/quantity/desc，与 _backend_roll_loot 输出结构一致，
        以便 grant_loot 统一落地。

        当前未实现，返回 (None, 原因)，让 generate_loot 回退后端生成。
        """
        return None, '拾荒 AI 产出接口尚未接入'

    # ── 落地 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _grant_one_loot_item(adv, item: dict) -> Tuple[Optional[str], Optional[str]]:
        """发放单件战利品；同名不可堆叠时最多重 roll 3 次。"""
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
        from plugins.adventurer_s1.s1_item_types import catalog_item_allows_quantity_stack_merge

        payload = dict(item or {})
        name = str(payload.get('name') or '').strip()
        if not name:
            return None, None

        item_type = str(payload.get('type') or 'misc')
        if catalog_item_allows_quantity_stack_merge(item_type):
            _, err = grant_raw_item(
                adv, name, payload,
                source_type='scavenge',
                description=f'拾荒所得：{name}',
            )
            if err:
                return None, err
            return name, None

        for attempt in range(3):
            if attempt > 0:
                rarity = str(payload.get('rarity') or 'common')
                pool = _LOOT_POOL.get(rarity) or _LOOT_POOL['common']
                tpl = dict(random.choice(pool))
                payload = {
                    'name': tpl['name'],
                    'type': tpl.get('type', 'misc'),
                    'rarity': rarity,
                    'quantity': 1,
                    'uses_left': tpl.get('uses_left'),
                    'desc': tpl.get('desc', ''),
                }
                name = payload['name']

            _, err = grant_raw_item(
                adv, name, payload,
                source_type='scavenge',
                description=f'拾荒所得：{name}',
            )
            if not err:
                return name, None
            if '已持有' not in str(err):
                return None, err
        return None, err

    @staticmethod
    def grant_loot(adv, loot: list[dict]) -> list[str]:
        """把战利品落地到冒险者随身/仓库。

        单件失败（如仓库满）记日志跳过、不中断——本方法在到期结算事务内调用，
        抛错会回滚整个结算并把冒险者卡在 building 状态。返回成功发放的物品名列表。
        """
        granted: list[str] = []
        for item in loot or []:
            name, err = S1ScavengeService._grant_one_loot_item(adv, item)
            if err:
                logger.warning('拾荒战利品发放失败 adv=%s item=%s: %s', adv.id, name or item, err)
                continue
            if name:
                granted.append(name)
        return granted
