"""S1 玩家物品保护锁。

保护锁只约束玩家主动处置，不属于物品世界设定，也不得进入任何 AI 语义输入。
"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
from datetime import datetime
from typing import Optional, Tuple

from db_repository import DatabaseError, db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1MarketBuyOffer,
    AdventurerS1MarketOffer,
    AdventurerS1WarehouseItem,
)


class _ItemProtectionDomainError(RuntimeError):
    """可直接展示给玩家的保护锁领域错误。"""


class S1ItemProtectionService:
    """原子切换实例保护状态，并撤销仍引用该仓库实例的待处理交易。"""

    SOURCE_EQUIPMENT = 'equipment'
    SOURCE_WAREHOUSE = 'warehouse'
    VALID_SOURCES = {SOURCE_EQUIPMENT, SOURCE_WAREHOUSE}

    @staticmethod
    def set_player_lock(
        adventurer_id: int,
        user_id: int,
        source: str,
        item_id: int,
        locked: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if source not in S1ItemProtectionService.VALID_SOURCES:
            return None, '物品来源无效'
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
            return None, '物品编号无效'
        if not isinstance(locked, bool):
            return None, 'locked 必须是布尔值'

        cancelled_count = 0
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                from plugins.adventurer_s1.services.adventurer_s1_service import (
                    AdventurerS1Service,
                )

                operation_err = AdventurerS1Service._check_inventory_operation_allowed(
                    adv,
                    user_id,
                )
                if operation_err:
                    raise _ItemProtectionDomainError(operation_err)

                model = AdventurerS1Equipment
                if source == S1ItemProtectionService.SOURCE_WAREHOUSE:
                    from plugins.adventurer_s1.services.s1_item_instance_service import (
                        lock_warehouse_aggregate,
                    )

                    lock_warehouse_aggregate(adventurer_id)
                    model = AdventurerS1WarehouseItem

                item = (
                    model.query
                    .filter_by(id=item_id, adventurer_id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if item is None:
                    location = '仓库' if source == S1ItemProtectionService.SOURCE_WAREHOUSE else '随身'
                    raise _ItemProtectionDomainError(f'该物品已不在{location}，请刷新后重试')

                item.is_player_locked = locked

                # 仓库出价并未移走原物品；锁定时必须在同一事务内撤销，避免旧出价绕过锁。
                if locked and source == S1ItemProtectionService.SOURCE_WAREHOUSE:
                    now = _portable_utc_now()
                    barter_offers = (
                        AdventurerS1MarketOffer.query
                        .filter_by(source_warehouse_item_id=item.id, status='pending')
                        .order_by(AdventurerS1MarketOffer.id.asc())
                        .populate_existing()
                        .with_for_update()
                        .all()
                    )
                    buy_offers = (
                        AdventurerS1MarketBuyOffer.query
                        .filter_by(source_warehouse_item_id=item.id, status='pending')
                        .order_by(AdventurerS1MarketBuyOffer.id.asc())
                        .populate_existing()
                        .with_for_update()
                        .all()
                    )
                    for offer in (*barter_offers, *buy_offers):
                        offer.status = 'cancelled'
                        offer.updated_at = now
                    cancelled_count = len(barter_offers) + len(buy_offers)
        except DatabaseError as exc:
            if isinstance(exc.original_error, _ItemProtectionDomainError):
                return None, str(exc.original_error)
            return None, player_operation_failure(exc, '设置物品保护失败')
        except _ItemProtectionDomainError as exc:
            return None, str(exc)
        except Exception as exc:
            return None, player_operation_failure(exc, '设置物品保护失败')

        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
        )

        invalidate_adventurer_detail_cache(adventurer_id)
        if cancelled_count:
            from plugins.adventurer_s1.services.adventurer_s1_market_service import (
                S1MarketService,
            )

            S1MarketService._emit_market_changed('item_protection_changed', 0)
        return {
            'source': source,
            'item_id': item_id,
            'is_player_locked': locked,
            'cancelled_market_offers': cancelled_count,
        }, None
