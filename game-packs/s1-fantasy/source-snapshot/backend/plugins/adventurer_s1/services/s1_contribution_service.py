"""深渊酒馆 S1 贡献度服务。

贡献度复用 ``currency_code='contribution'``（per-adventurer，随冒险者阵亡自然清零），
所有变动走 :class:`S1CurrencyService`，审计落 ``AdventurerS1CurrencyLog``。

来源换算：
- **捐献材料**：按获得该材料的等效体力 1:1 换算贡献；直接采集物默认 3/个（30 体力出 10），
  产业链材料（木炭、铁锭等）见 ``ADVENTURER_S1_CONTRIBUTION_STAMINA_BY_MATERIAL``。
- **帮忙建造**：按实际花费的体力 1:1 换算；实时累加在 ``_tick_building_progress``，
  历史折算用 ``help_seconds_to_contribution`` 反推。

GM「一键校准」:func:`reconcile_all` 把累计贡献度对账到建筑贡献榜应得值
（``compute_historical_targets``）、可消耗贡献度对账到「应得 − 净消费」，双向精确、幂等。
"""
from runtime_core.time import utc_now as _portable_utc_now

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1Currency,
    AdventurerS1CurrencyLog,
    AdventurerS1BuildingDonation,
    AdventurerS1BuildingHelper,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

logger = logging.getLogger(__name__)

CONTRIBUTION_CODE = 'contribution'            # 当前可消耗贡献值（接委托扣减）
CONTRIBUTION_TOTAL_CODE = 'contribution_total'  # 累计获得总贡献值（建筑贡献榜应得）

# 影响「可消耗贡献值」的消费/退还流水来源。
# 净消费 = 接委托扣减(记负) + 取消退还(记正) 取负求和。
# 仅枚举真实消费途径，刻意排除校准等系统调账，保证幂等。
_CONSUMPTION_SOURCES = ('commission_accept', 'commission_cancel')
_RECONCILE_SOURCE = 'contribution_reconcile'


class S1ContributionService:
    """贡献度统一接口：累加 / 扣减 / 换算 / 一键补发。"""

    CURRENCY_CODE = CONTRIBUTION_CODE

    # ── 配置 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def per_material() -> int:
        """每捐 1 个直接采集材料换算的贡献度（默认 3）。"""
        try:
            return int(current_app.config.get('ADVENTURER_S1_CONTRIBUTION_PER_MATERIAL', 3) or 3)
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def stamina_per_donated_material(material_name: str) -> float:
        """捐献 1 个 material_name 应换算的等效体力（= 贡献度）。"""
        overrides = current_app.config.get('ADVENTURER_S1_CONTRIBUTION_STAMINA_BY_MATERIAL') or {}
        if material_name in overrides:
            try:
                return float(overrides[material_name])
            except (TypeError, ValueError):
                pass
        return float(S1ContributionService.per_material())

    @staticmethod
    def donation_contribution(material_name: str, amount) -> int:
        """捐献 amount 个 material_name 应获得的贡献度。"""
        try:
            qty = int(amount or 0)
        except (TypeError, ValueError):
            return 0
        if qty <= 0:
            return 0
        per_unit = S1ContributionService.stamina_per_donated_material(material_name)
        return int(round(qty * per_unit))

    @staticmethod
    def _building_stamina_per_hour(template) -> int:
        if template and template.stamina_cost_per_help:
            return int(template.stamina_cost_per_help)
        try:
            return int(current_app.config.get('ADVENTURER_S1_BUILDING_STAMINA_PER_HOUR', 20) or 20)
        except (TypeError, ValueError):
            return 20

    @staticmethod
    def _building_speedup_minutes(template) -> int:
        if template and template.speedup_minutes:
            return int(template.speedup_minutes)
        try:
            return int(current_app.config.get('ADVENTURER_S1_BUILDING_SPEEDUP_MINUTES', 60) or 60)
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def help_seconds_to_contribution(contributed_seconds, stamina_per_hour, speedup_minutes) -> int:
        """建筑进度秒(contributed_seconds) → 实际花费体力 = 贡献。

        与 ``_tick_building_progress`` 的 ``progress_added = stamina × (speedup_minutes×60/stamina_per_hour)``
        互为逆运算，无论 speedup 多少都恒等于实际花费体力。
        """
        if not contributed_seconds or contributed_seconds <= 0:
            return 0
        sph = int(stamina_per_hour or 20) or 20
        spm = int(speedup_minutes or 60) or 60
        return int(round(float(contributed_seconds) * sph / (spm * 60)))

    # ── 余额 / 变动 ────────────────────────────────────────────────────────────

    @staticmethod
    def get_balance(adventurer_id: int) -> int:
        """当前可消耗贡献值。"""
        bal, _ = S1CurrencyService.get_balance(adventurer_id, CONTRIBUTION_CODE)
        return int(bal or 0)

    @staticmethod
    def get_total_balance(adventurer_id: int) -> int:
        """累计获得总贡献值（只增不减）。"""
        bal, _ = S1CurrencyService.get_balance(adventurer_id, CONTRIBUTION_TOTAL_CODE)
        return int(bal or 0)

    @staticmethod
    def accrue(adventurer_id, amount, source_type, related_id=None,
               related_type=None, description=None, auto_commit=False):
        """累加贡献度：同时记当前值(可消耗)与总值(累计获得、只增不减)。amount<=0 跳过。

        默认在已有事务内、不自动 commit。
        """
        amount = int(round(amount or 0))
        if amount <= 0:
            return None, None
        # 当前值：先不 commit，等总值一起落
        res = S1CurrencyService.add(
            adventurer_id, CONTRIBUTION_CODE, float(amount),
            source_type=source_type, related_id=related_id, related_type=related_type,
            description=description, auto_commit=False,
        )
        # 总值并行累加（只增不减）
        S1CurrencyService.add(
            adventurer_id, CONTRIBUTION_TOTAL_CODE, float(amount),
            source_type=source_type, related_id=related_id, related_type=related_type,
            description=description, auto_commit=auto_commit,
        )
        # 贡献每日任务以贡献流水为进度源；达标后立即结算，避免玩家再手动领取。
        try:
            from plugins.adventurer_s1.services.adventurer_s1_task_service import (
                AdventurerS1TaskService,
            )
            AdventurerS1TaskService.settle_contribution_task(
                adventurer_id, auto_commit=auto_commit,
            )
        except Exception:
            logger.exception('自动结算贡献每日任务失败: adventurer_id=%s', adventurer_id)
        return res

    @staticmethod
    def spend(adventurer_id, amount, source_type, related_id=None,
              related_type=None, description=None, auto_commit=False):
        """扣除贡献度。返回 (new_balance, error)。"""
        return S1CurrencyService.deduct(
            adventurer_id, CONTRIBUTION_CODE, float(amount),
            source_type=source_type, related_id=related_id, related_type=related_type,
            description=description, auto_commit=auto_commit,
        )

    # ── GM 一键校准 ────────────────────────────────────────────────────────────

    @staticmethod
    def compute_historical_targets() -> dict:
        """聚合所有冒险者的历史贡献度目标值 {adventurer_id: contribution}。"""
        target: dict[int, int] = {}

        # 1) 捐献历史：按冒险者 + 材料类型聚合
        donation_rows = (
            db.session.query(
                AdventurerS1BuildingDonation.adventurer_id,
                AdventurerS1BuildingDonation.material_name,
                func.coalesce(func.sum(AdventurerS1BuildingDonation.amount), 0),
            )
            .filter(AdventurerS1BuildingDonation.adventurer_id.isnot(None))
            .group_by(
                AdventurerS1BuildingDonation.adventurer_id,
                AdventurerS1BuildingDonation.material_name,
            )
            .all()
        )
        db_manager.rollback()
        for adv_id, material_name, total in donation_rows:
            contrib = S1ContributionService.donation_contribution(material_name, int(total or 0))
            if contrib > 0:
                target[adv_id] = target.get(adv_id, 0) + contrib

        # 2) 帮工历史：按主键分页。每页查询后立即结束只读事务，再做历史折算，
        # 避免全量扫描和 Python 计算期间长期保持 idle-in-transaction。
        now = _portable_utc_now()
        last_helper_id = 0
        while True:
            helper_rows = (
                db.session.query(
                    AdventurerS1BuildingHelper.id,
                    AdventurerS1BuildingHelper.adventurer_id,
                    AdventurerS1BuildingHelper.helped_at,
                    AdventurerS1BuildingHelper.left_at,
                    AdventurerS1BuildingHelper.contributed_seconds,
                    AdventurerS1BuildingTemplate.stamina_cost_per_help,
                    AdventurerS1BuildingTemplate.speedup_minutes,
                )
                .outerjoin(
                    AdventurerS1BuildingInstance,
                    AdventurerS1BuildingInstance.id
                    == AdventurerS1BuildingHelper.building_instance_id,
                )
                .outerjoin(
                    AdventurerS1BuildingTemplate,
                    AdventurerS1BuildingTemplate.id
                    == AdventurerS1BuildingInstance.template_id,
                )
                .filter(AdventurerS1BuildingHelper.id > last_helper_id)
                .order_by(AdventurerS1BuildingHelper.id.asc())
                .limit(500)
                .all()
            )
            db_manager.rollback()
            if not helper_rows:
                break
            last_helper_id = helper_rows[-1][0]
            for (
                _helper_id,
                adv_id,
                helped_at,
                left_at,
                contributed_seconds,
                stamina_cost_per_help,
                speedup_minutes,
            ) in helper_rows:
                if not adv_id:
                    continue
                template = SimpleNamespace(
                    stamina_cost_per_help=stamina_cost_per_help,
                    speedup_minutes=speedup_minutes,
                )
                sph = S1ContributionService._building_stamina_per_hour(template)
                spm = S1ContributionService._building_speedup_minutes(template)
                if contributed_seconds is not None:
                    contrib = S1ContributionService.help_seconds_to_contribution(
                        contributed_seconds, sph, spm,
                    )
                else:
                    # 历史 NULL（精确入账上线前的记录）：按在场实时长 × 每小时体力估算
                    end = left_at or now
                    real_seconds = max(0, int((end - helped_at).total_seconds())) if helped_at else 0
                    contrib = int(round(real_seconds / 3600.0 * sph))
                if contrib > 0:
                    target[adv_id] = target.get(adv_id, 0) + contrib
        return target

    @staticmethod
    def compute_net_spent() -> dict:
        """从流水统计每个冒险者「可消耗贡献值」的净消费 {adventurer_id: net_spent}。

        净消费 = 接委托扣减(记负) + 取消退还(记正)，取负求和。仅统计真实消费
        来源(:data:`_CONSUMPTION_SOURCES`)，刻意排除校准等系统调账，保证幂等。
        """
        rows = (
            db.session.query(
                AdventurerS1CurrencyLog.adventurer_id,
                func.coalesce(func.sum(AdventurerS1CurrencyLog.change_amount), 0),
            )
            .filter(
                AdventurerS1CurrencyLog.currency_code == CONTRIBUTION_CODE,
                AdventurerS1CurrencyLog.source_type.in_(_CONSUMPTION_SOURCES),
            )
            .group_by(AdventurerS1CurrencyLog.adventurer_id)
            .all()
        )
        db_manager.rollback()
        return {adv_id: -float(s or 0) for adv_id, s in rows}

    @staticmethod
    def reconcile_all(updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """一键校准：把每个冒险者的贡献度精确设置为完全正确的值（双向，可增可减）。

        - 累计贡献度(contribution_total) = 建筑贡献榜应得 ``target``（捐献 + 帮工折算，
          由 :meth:`compute_historical_targets` 从源头重算）。
        - 可消耗贡献度(contribution) = ``target − 净消费``（接委托 − 取消退还）。

        对账到精确目标值：高了下扣、低了补足，一步到位。``allow_negative=True`` 允许
        扣成负数——玩家曾用历史多发的虚高额度超额消费时，可消耗值如实为负=透支。
        幂等：再次执行各项差额≈0；校准流水写 ``contribution_reconcile``，不计入净消费。
        """
        target = S1ContributionService.compute_historical_targets()
        net_spent = S1ContributionService.compute_net_spent()
        existing_current = {
            r.adventurer_id: float(r.amount)
            for r in AdventurerS1Currency.query.filter_by(currency_code=CONTRIBUTION_CODE).all()
        }
        existing_total = {
            r.adventurer_id: float(r.amount)
            for r in AdventurerS1Currency.query.filter_by(currency_code=CONTRIBUTION_TOTAL_CODE).all()
        }
        db_manager.rollback()

        adv_ids = set(target) | set(existing_current) | set(existing_total)

        affected = 0
        net_change = 0  # 可消耗值净变动（正=净补足，负=净扣回）
        try:
            with db_manager.transaction():
                for adv_id in adv_ids:
                    correct_total = round(float(target.get(adv_id, 0)), 2)
                    correct_current = round(correct_total - net_spent.get(adv_id, 0.0), 2)
                    changed = False
                    for code, correct, cur in (
                        (CONTRIBUTION_TOTAL_CODE, correct_total, existing_total.get(adv_id, 0.0)),
                        (CONTRIBUTION_CODE, correct_current, existing_current.get(adv_id, 0.0)),
                    ):
                        diff = round(correct - cur, 2)
                        if abs(diff) < 0.5:
                            continue
                        if diff > 0:
                            S1CurrencyService.add(
                                adv_id, code, diff, source_type=_RECONCILE_SOURCE,
                                description='贡献度一键校准', auto_commit=False,
                            )
                        else:
                            S1CurrencyService.deduct(
                                adv_id, code, -diff, source_type=_RECONCILE_SOURCE,
                                description='贡献度一键校准', auto_commit=False,
                                allow_negative=True,
                            )
                        if code == CONTRIBUTION_CODE:
                            net_change += int(diff)
                        changed = True
                    if changed:
                        affected += 1
        except Exception as e:
            logger.exception('贡献度一键校准失败')
            return None, f'校准失败：{e}'

        return {
            'adventurers_affected': affected,
            'contribution_net_change': net_change,
            'per_material': S1ContributionService.per_material(),
        }, None
