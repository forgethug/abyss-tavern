"""哥布林矿坑藏宝图：接取任务后在建造帮工 / 采集中随机掉落（全服限量）。

掉落概率与本次消耗的精力成正比，采集 / 帮工使用独立倍率（帮工略优待）：
  采集 prob = PROB_PER_GATHERING_STAMINA × stamina_spent  → 期望约 12h 在线采集 / 张
  帮工 prob = PROB_PER_BUILDING_STAMINA × stamina_spent   → 期望约 10h 在线帮工 / 张

（以上按当前 60 精力/h 采集、20 精力/h 帮工估算；仍受全服 5 张上限约束。）
"""
import logging
import random

from flask import current_app

logger = logging.getLogger(__name__)

# 期望在线时长反推：12h×60 精力/h → 720；10h×20 精力/h → 200
_DEFAULT_PROB_PER_GATHERING_STAMINA = 1 / 720
_DEFAULT_PROB_PER_BUILDING_STAMINA = 1 / 200


class AdventurerS1TreasureMapDropService:
    """方案 B：须先接取 treasure_hunt 任务，帮工 tick 或采集完成时掷骰发奖。"""

    @staticmethod
    def task_template_id() -> str:
        return str(current_app.config.get(
            'ADVENTURER_S1_TREASURE_MAP_TASK_ID',
            'goblin_mine_treasure_hunt',
        ))

    @staticmethod
    def _prob_per_gathering_stamina() -> float:
        raw = current_app.config.get(
            'ADVENTURER_S1_TREASURE_MAP_DROP_PROB_PER_GATHERING_STAMINA',
            _DEFAULT_PROB_PER_GATHERING_STAMINA,
        )
        return max(0.0, float(raw))

    @staticmethod
    def _prob_per_building_stamina() -> float:
        raw = current_app.config.get(
            'ADVENTURER_S1_TREASURE_MAP_DROP_PROB_PER_BUILDING_STAMINA',
            _DEFAULT_PROB_PER_BUILDING_STAMINA,
        )
        return max(0.0, float(raw))

    @staticmethod
    def drop_prob_for_gathering_stamina(stamina_spent: int) -> float:
        spent = max(0, int(stamina_spent or 0))
        if spent <= 0:
            return 0.0
        return min(1.0, AdventurerS1TreasureMapDropService._prob_per_gathering_stamina() * spent)

    @staticmethod
    def drop_prob_for_building_stamina(stamina_spent: int) -> float:
        spent = max(0, int(stamina_spent or 0))
        if spent <= 0:
            return 0.0
        return min(1.0, AdventurerS1TreasureMapDropService._prob_per_building_stamina() * spent)

    @staticmethod
    def try_roll_building_stamina(stamina_by_adventurer: dict[int, int]) -> None:
        """建造帮工 tick 后调用：按本 tick 各帮工实际消耗的精力掷骰。"""
        if not stamina_by_adventurer:
            return
        from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService

        adv_ids = [aid for aid, st in stamina_by_adventurer.items() if st > 0]
        assignments = AdventurerS1TaskService.treasure_hunt_assignments(
            AdventurerS1TreasureMapDropService.task_template_id(), adv_ids,
        )
        if not assignments:
            return

        ended_task_ids = {
            task_id
            for task_id in set(assignments.values())
            if AdventurerS1TaskService.is_treasure_hunt_ended(task_id)
        }
        for task_id in ended_task_ids:
            AdventurerS1TaskService.expire_remaining_accepted_tasks(task_id)

        for adv_id in adv_ids:
            task_id = assignments.get(adv_id)
            if not task_id or task_id in ended_task_ids:
                continue
            prob = AdventurerS1TreasureMapDropService.drop_prob_for_building_stamina(
                stamina_by_adventurer[adv_id],
            )
            if prob <= 0 or random.random() >= prob:
                continue
            AdventurerS1TreasureMapDropService._award_if_won(adv_id, task_id)

    @staticmethod
    def try_roll_gathering_complete(adventurer_id: int, *, stamina_spent: int) -> None:
        """采集结算完成后调用：按本轮 usage_stamina_cost 掷骰。"""
        from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService

        assignments = AdventurerS1TaskService.treasure_hunt_assignments(
            AdventurerS1TreasureMapDropService.task_template_id(), [adventurer_id],
        )
        task_id = assignments.get(adventurer_id)
        if not task_id:
            return
        if AdventurerS1TaskService.is_treasure_hunt_ended(task_id):
            AdventurerS1TaskService.expire_remaining_accepted_tasks(task_id)
            return

        prob = AdventurerS1TreasureMapDropService.drop_prob_for_gathering_stamina(stamina_spent)
        if prob <= 0 or random.random() >= prob:
            return
        AdventurerS1TreasureMapDropService._award_if_won(adventurer_id, task_id)

    @staticmethod
    def _award_if_won(adventurer_id: int, task_template_id: str) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService

        awarded, err = AdventurerS1TaskService.finalize_treasure_hunt_drop(
            adventurer_id, task_template_id,
        )
        if err:
            logger.debug(
                '藏宝图掉落未入账 adv=%s task=%s err=%s',
                adventurer_id, task_template_id, err,
            )
            return
        if awarded:
            logger.info('藏宝图掉落成功 adv=%s task=%s', adventurer_id, task_template_id)
