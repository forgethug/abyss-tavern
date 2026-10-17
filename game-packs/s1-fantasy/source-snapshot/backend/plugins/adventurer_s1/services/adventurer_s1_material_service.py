"""深渊酒馆 S1 材料库存管理统一接口。

材料（wood / stone / ...）是冒险者持有的独立物品库存：
- 不允许装备（不进 AdventurerS1Equipment 表）
- 不允许带入副本（不进 prompt，探险结算不消耗）
- 主要消耗场景：宿舍楼开工时按 owner 名下汇总扣

所有材料变动必须通过本服务，禁止直接修改 AdventurerS1MaterialItem.quantity。
"""

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_database_result, player_operation_failure,
)
import logging
from typing import Optional, Tuple, List
from sqlalchemy import func

from db_repository import db_manager, DatabaseError, ConcurrencyError
from db_repository.decorators import db_retry
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1MaterialItem, AdventurerS1MaterialLog,
)

logger = logging.getLogger(__name__)


# 材料元数据：用于前端展示（图标 + 中文名）。
# 命名沿用 `wood` / `stone` 等英文 code 作为内部稳定标识；中文/icon 仅用于序列化展示。
# 后续新增材料只需在此追加一项。
MATERIAL_META = {
    'wood': {'name': '木材', 'icon': '🪵'},
    'stone': {'name': '石料', 'icon': '🪨'},
    'charcoal': {'name': '木炭', 'icon': '⚫'},
    'iron_ore': {'name': '铁矿石', 'icon': '⛏️'},
    'iron_ingot': {'name': '铁锭', 'icon': '🔩'},
}


class S1MaterialService:
    """深渊酒馆 S1 材料库存管理统一接口。"""

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_amount(amount) -> Tuple[Optional[int], Optional[str]]:
        """材料按整数计量，浮点会被截断为 int 报错。"""
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None, '数量必须为整数'
        if amount != int(amount):
            return None, '材料数量必须为整数'
        amt = int(amount)
        if abs(amt) > 1_000_000_000:
            return None, '数量超出允许范围'
        return amt, None

    @staticmethod
    def _log(adventurer_id: int, name: str, change_amount: int,
             balance_after: int, source_type: str,
             related_id=None, related_type=None, description=None,
             silent: bool = False):
        """写入变动日志。必须在事务内调用。silent=True 时跳过（GM 隐身）。"""
        if silent:
            return
        # VARCHAR(200) 防截断：AI 生成的 reason 可能超长
        if description and len(description) > 200:
            description = description[:197] + '...'
        log = AdventurerS1MaterialLog(
            adventurer_id=adventurer_id,
            name=name,
            change_amount=int(change_amount),
            balance_after=int(balance_after),
            source_type=source_type,
            related_id=str(related_id) if related_id is not None else None,
            related_type=related_type,
            description=description,
        )
        db_manager.add(log)

    # ── 查询 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def get_balance(adventurer_id: int, name: str) -> Tuple[Optional[int], Optional[str]]:
        """查询单个冒险者的某材料余额。"""
        try:
            rec = AdventurerS1MaterialItem.query.filter_by(
                adventurer_id=adventurer_id, name=name,
            ).first()
            return int(rec.quantity) if rec else 0, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def get_all_balances(adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """查询单个冒险者的所有材料余额，返回 {name: quantity}。

        会用 MATERIAL_META 的全部 key 兜底为 0，方便前端直接展示进度条不用空值判断。
        """
        try:
            recs = AdventurerS1MaterialItem.query.filter_by(adventurer_id=adventurer_id).all()
            result = {r.name: int(r.quantity) for r in recs}
            for code in MATERIAL_META:
                result.setdefault(code, 0)
            return result, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def get_owner_totals(owner_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """查询 owner 名下所有冒险者的材料汇总，返回 {name: total_quantity}。

        宿舍楼面板展示和开工扣材料校验都依赖这个聚合数据。
        """
        try:
            recs = (
                db_manager.query(
                    AdventurerS1MaterialItem.name,
                    func.sum(AdventurerS1MaterialItem.quantity),
                )
                .join(AdventurerS1, AdventurerS1.id == AdventurerS1MaterialItem.adventurer_id)
                .filter(
                    AdventurerS1.owner_id == owner_id,
                    AdventurerS1.is_player_created == True,
                )
                .group_by(AdventurerS1MaterialItem.name)
                .all()
            )
            totals = {code: 0 for code in MATERIAL_META}
            for name, quantity in recs:
                totals[name] = int(quantity)
            return totals, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    # ── 增减 ──────────────────────────────────────────────────────────────────

    # 核心写入逻辑用 @db_retry 自动重试 ConcurrencyError / OperationalError；
    # 外层 add/deduct 把异常转回项目统一的 (result, error) 元组契约。

    @staticmethod
    @db_retry()
    def _add_impl(adventurer_id: int, name: str, amount: int,
                  source_type: str, related_id, related_type, description,
                  auto_commit: bool, silent: bool = False) -> Tuple[Optional[int], Optional[str]]:
        validated, err = S1MaterialService._validate_amount(amount)
        if err:
            return None, err
        amount = validated
        if amount < 0:
            return None, '增加数量必须 ≥ 0'
        if amount == 0:
            cur, e = S1MaterialService.get_balance(adventurer_id, name)
            return cur, e

        rec = AdventurerS1MaterialItem.query.filter_by(
            adventurer_id=adventurer_id, name=name,
        ).with_for_update().first()
        if rec is None:
            rec = AdventurerS1MaterialItem(
                adventurer_id=adventurer_id, name=name, quantity=0,
            )
            ok, err = player_database_result(db_manager.add(rec))
            if not ok:
                return None, err

        rec.quantity = int(rec.quantity) + amount
        S1MaterialService._log(
            adventurer_id=adventurer_id, name=name,
            change_amount=amount, balance_after=rec.quantity,
            source_type=source_type, related_id=related_id,
            related_type=related_type, description=description,
            silent=silent,
        )

        if auto_commit:
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
        return int(rec.quantity), None

    @staticmethod
    def add(adventurer_id: int, name: str, amount: int,
            source_type: str, related_id=None, related_type=None, description=None,
            auto_commit: bool = True, silent: bool = False) -> Tuple[Optional[int], Optional[str]]:
        """增加材料数量。返回 (new_quantity, error)。"""
        try:
            return S1MaterialService._add_impl(
                adventurer_id, name, amount, source_type,
                related_id, related_type, description, auto_commit, silent,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    @db_retry()
    def _deduct_impl(adventurer_id: int, name: str, amount: int,
                     source_type: str, related_id, related_type, description,
                     auto_commit: bool, silent: bool = False) -> Tuple[Optional[int], Optional[str]]:
        validated, err = S1MaterialService._validate_amount(amount)
        if err:
            return None, err
        amount = validated
        if amount <= 0:
            return None, '扣减数量必须 > 0'

        rec = AdventurerS1MaterialItem.query.filter_by(
            adventurer_id=adventurer_id, name=name,
        ).with_for_update().first()
        if rec is None or int(rec.quantity) < amount:
            return None, f'{name} 库存不足'

        rec.quantity = int(rec.quantity) - amount
        S1MaterialService._log(
            adventurer_id=adventurer_id, name=name,
            change_amount=-amount, balance_after=rec.quantity,
            source_type=source_type, related_id=related_id,
            related_type=related_type, description=description,
            silent=silent,
        )

        if auto_commit:
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
        return int(rec.quantity), None

    @staticmethod
    def deduct(adventurer_id: int, name: str, amount: int,
               source_type: str, related_id=None, related_type=None, description=None,
               auto_commit: bool = True, silent: bool = False) -> Tuple[Optional[int], Optional[str]]:
        """扣减材料数量。库存不足返回错误。返回 (new_quantity, error)。"""
        try:
            return S1MaterialService._deduct_impl(
                adventurer_id, name, amount, source_type,
                related_id, related_type, description, auto_commit, silent,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    # ── 元数据 ────────────────────────────────────────────────────────────────

    @staticmethod
    def get_material_meta() -> dict:
        """返回所有材料的元数据 {code: {name, icon}}，前端用于展示。"""
        return {code: dict(meta) for code, meta in MATERIAL_META.items()}
