"""深渊酒馆 S1 隐秘拍卖行。

规则：
- 管理员批量上架自定义拍品并设定开奖时间
- 玩家密封出价，金币预先扣除；他人仅可见出价人数
- 到期最高者得，其余退钱；最高出价同价则流拍（全员退钱）
- 每位冒险者对同一拍品仅保留一条出价（可改价：先退旧扣新）
"""
from runtime_core.time import utc_now as _portable_utc_now
import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import String, cast, func

from db_repository import ConcurrencyError, db_manager
from db_repository.decorators import db_retry, with_transaction
from utils.pagination import build_paginated_response
from sqlalchemy.orm import joinedload

from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Item,
    AdventurerS1SecretAuctionLot,
    AdventurerS1SecretAuctionBid,
    AdventurerS1WarehouseItem,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1CurrencyLog,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_item_grant_service import _default_uses_left, grant_raw_item
from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity
from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregates
from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_immutable_item_names
from plugins.adventurer_s1.s1_item_types import S1_ITEM_TYPE_SET
from utils.cache_manager import CacheManager

logger = logging.getLogger(__name__)

_open_lot_summary_cache = CacheManager(ttl_seconds=60, max_size=1)


def invalidate_open_lot_summary_cache() -> None:
    """拍卖行进行中拍品数缓存失效（上架/结算/取消后调用）。"""
    _open_lot_summary_cache.clear()


def _sanitize_player_lot_item(item: dict, lot: AdventurerS1SecretAuctionLot) -> None:
    if str(lot.item_type or '').strip() != 'clue':
        return
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        sanitize_player_catalog_item_dict,
    )

    sanitize_player_catalog_item_dict(
        item,
        {'catalog_name': lot.item_name},
        name_key='item_name',
        desc_key='desc',
        type_key='item_type' if 'item_type' in item else None,
    )


class S1SecretAuctionService:
    """隐秘拍卖行：管理员上架 / 玩家出价 / 定时结算。"""

    VALID_RARITIES = ('common', 'rare', 'epic', 'legendary')
    VALID_STATUSES = ('open', 'settled_won', 'settled_pass', 'cancelled')
    HISTORY_STATUSES = ('settled_won', 'settled_pass', 'cancelled')
    READONLY_OPEN_BID_FIELDS = (
        'id',
        'adventurer_id',
        'adventurer_name',
        'status',
        'created_at',
    )
    CURRENCY_CODE = 'gold'
    LEDGER_SOURCE_TYPES = (
        'secret_auction_bid',
        'secret_auction_refund',
        'secret_auction_reconcile',
    )

    # ── 内部工具 ────────────────────────────────────────────────────────────

    @staticmethod
    def _lock_adventurers(adventurer_ids) -> dict[int, AdventurerS1]:
        normalized_ids = sorted({int(value) for value in (adventurer_ids or [])})
        if not normalized_ids:
            return {}
        rows = (
            AdventurerS1.query
            .filter(AdventurerS1.id.in_(normalized_ids))
            .order_by(AdventurerS1.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        return {row.id: row for row in rows}

    @classmethod
    def _lock_held_bids(
        cls,
        lot_id: int,
        *,
        lock_warehouses: bool = False,
    ) -> tuple[dict[int, AdventurerS1], list[AdventurerS1SecretAuctionBid]]:
        projections = (
            AdventurerS1SecretAuctionBid.query
            .with_entities(
                AdventurerS1SecretAuctionBid.id,
                AdventurerS1SecretAuctionBid.adventurer_id,
            )
            .filter_by(lot_id=lot_id, status='held')
            .order_by(AdventurerS1SecretAuctionBid.id.asc())
            .all()
        )
        adventurer_ids = sorted({int(row.adventurer_id) for row in projections})
        adventurer_map = cls._lock_adventurers(adventurer_ids)
        if lock_warehouses:
            lock_warehouse_aggregates(adventurer_ids)
        bid_ids = [int(row.id) for row in projections]
        bids = (
            AdventurerS1SecretAuctionBid.query
            .filter(
                AdventurerS1SecretAuctionBid.id.in_(bid_ids),
                AdventurerS1SecretAuctionBid.status == 'held',
            )
            .order_by(AdventurerS1SecretAuctionBid.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
            if bid_ids else []
        )
        return adventurer_map, bids

    @staticmethod
    def _parse_ends_at(raw) -> Tuple[Optional[datetime], Optional[str]]:
        if not raw:
            return None, '缺少开奖时间 ends_at'
        try:
            text = str(raw).strip().replace('Z', '+00:00')
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt, None
        except (TypeError, ValueError):
            return None, '开奖时间格式无效'

    @staticmethod
    def _require_auction_built() -> Optional[str]:
        built = (
            AdventurerS1BuildingInstance.query
            .join(AdventurerS1BuildingTemplate)
            .filter(
                AdventurerS1BuildingTemplate.handler_key == 'secret_auction',
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.status == 'built',
                AdventurerS1BuildingInstance.disabled.is_(False),
            ).first()
        )
        if not built:
            return '隐秘拍卖行尚未建成，暂无法参与'
        return None

    @staticmethod
    def _get_owned_adventurer(adventurer_id: int, user_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '无权操作此冒险者'
        can, reason = adv.can_dispatch()
        if not can:
            return None, reason or '当前状态无法参与拍卖'
        return adv, None

    @staticmethod
    def _resolve_owned_adventurer_id(
        user_id: int, adventurer_id: Optional[int],
    ) -> Tuple[Optional[int], Optional[str]]:
        """列表/历史接口：仅允许查看本人冒险者的密封出价。"""
        if adventurer_id is None:
            return None, None
        adv, err = S1SecretAuctionService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err
        return adv.id, None

    @staticmethod
    def _bid_count_map(lot_ids: list[int]) -> dict[int, int]:
        if not lot_ids:
            return {}
        bids = (
            AdventurerS1SecretAuctionBid.query
            .filter(
                AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids),
                AdventurerS1SecretAuctionBid.status == 'held',
            )
            .all()
        )
        funded = S1SecretAuctionService._funded_held_bids(bids)
        result: dict[int, int] = {}
        for bid in funded:
            result[bid.lot_id] = result.get(bid.lot_id, 0) + 1
        return result

    @staticmethod
    def _escrow_amount_map(bids: list[AdventurerS1SecretAuctionBid]) -> dict[tuple[int, int], float]:
        """从不可变货币流水计算每个出价实际仍在托管的金额。

        bid.amount/status 曾被历史 bug 污染，不能单独作为退款和结算依据。
        """
        if not bids:
            return {}
        lot_ids = {str(b.lot_id) for b in bids}
        adventurer_ids = {b.adventurer_id for b in bids}
        rows = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                AdventurerS1CurrencyLog.related_id.in_(lot_ids),
                AdventurerS1CurrencyLog.source_type.in_(
                    S1SecretAuctionService.LEDGER_SOURCE_TYPES,
                ),
            )
            .with_entities(
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.adventurer_id,
                func.sum(AdventurerS1CurrencyLog.change_amount),
            )
            .group_by(
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.adventurer_id,
            )
            .all()
        )
        return {
            (int(lot_id), adventurer_id): round(max(0.0, -float(net or 0)), 2)
            for lot_id, adventurer_id, net in rows
        }

    @staticmethod
    def _funded_held_bids(
        bids: list[AdventurerS1SecretAuctionBid],
    ) -> list[AdventurerS1SecretAuctionBid]:
        escrow = S1SecretAuctionService._escrow_amount_map(bids)
        return [
            bid for bid in bids
            if bid.status == 'held'
            and abs(escrow.get((bid.lot_id, bid.adventurer_id), 0.0) - round(float(bid.amount), 2)) < 0.001
        ]

    @staticmethod
    def _ledger_net(lot_id: int, adventurer_id: int) -> float:
        net = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                AdventurerS1CurrencyLog.related_id == str(lot_id),
                AdventurerS1CurrencyLog.source_type.in_(
                    S1SecretAuctionService.LEDGER_SOURCE_TYPES,
                ),
            )
            .with_entities(func.sum(AdventurerS1CurrencyLog.change_amount))
            .scalar()
        )
        return round(float(net or 0), 2)

    @staticmethod
    @with_transaction(auto_commit=True)
    def _reconcile_account(lot_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """修复一个拍品账户。锁冒险者行后重算，保证多 worker 幂等。"""
        adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
        if not adv:
            return {'reclaimed': 0.0, 'invalidated': 0}, None
        bid = (
            AdventurerS1SecretAuctionBid.query
            .filter_by(lot_id=lot_id, adventurer_id=adventurer_id)
            .with_for_update().first()
        )

        net = S1SecretAuctionService._ledger_net(lot_id, adventurer_id)
        reclaimed = 0.0
        if net > 0:
            _, err = S1CurrencyService.deduct(
                adventurer_id,
                S1SecretAuctionService.CURRENCY_CODE,
                net,
                source_type='secret_auction_reconcile',
                related_id=lot_id,
                related_type='secret_auction_lot',
                description=f'隐秘拍卖历史异常退款自动纠正（拍品#{lot_id}）',
                auto_commit=False,
                allow_negative=True,
            )
            if err:
                db_manager.rollback()
                return None, err
            reclaimed = net

        invalidated = 0
        if bid and bid.status == 'held':
            escrow = max(0.0, -S1SecretAuctionService._ledger_net(lot_id, adventurer_id))
            if abs(escrow - round(float(bid.amount), 2)) >= 0.001:
                err = S1SecretAuctionService._refund_bid(
                    bid,
                    lot_id,
                    f'历史异常出价自动作废退还（拍品#{lot_id}）',
                )
                if err:
                    db_manager.rollback()
                    return None, err
                invalidated = 1

        return {'reclaimed': reclaimed, 'invalidated': invalidated}, None

    @staticmethod
    def reconcile_dirty_data(limit: int = 200) -> dict:
        """幂等清理正式服历史脏数据，由 scheduler 周期执行。"""
        limit = max(1, min(int(limit or 200), 1000))
        positive_rows = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                AdventurerS1CurrencyLog.source_type.in_(
                    S1SecretAuctionService.LEDGER_SOURCE_TYPES,
                ),
            )
            .with_entities(
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.adventurer_id,
                func.sum(AdventurerS1CurrencyLog.change_amount).label('net'),
            )
            .group_by(
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.adventurer_id,
            )
            .having(func.sum(AdventurerS1CurrencyLog.change_amount) > 0.001)
            .limit(limit).all()
        )
        candidates: set[tuple[int, int]] = set()
        for lot_id, adventurer_id, _ in positive_rows:
            try:
                candidates.add((int(lot_id), adventurer_id))
            except (TypeError, ValueError):
                logger.warning('忽略无效拍卖流水 related_id=%r', lot_id)

        remaining = max(0, limit - len(candidates))
        if remaining:
            ledger = (
                AdventurerS1CurrencyLog.query
                .filter(
                    AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                    AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                    AdventurerS1CurrencyLog.source_type.in_(
                        S1SecretAuctionService.LEDGER_SOURCE_TYPES,
                    ),
                )
                .with_entities(
                    AdventurerS1CurrencyLog.related_id,
                    AdventurerS1CurrencyLog.adventurer_id,
                    func.sum(AdventurerS1CurrencyLog.change_amount).label('net'),
                )
                .group_by(
                    AdventurerS1CurrencyLog.related_id,
                    AdventurerS1CurrencyLog.adventurer_id,
                )
                .subquery()
            )
            dirty_held = (
                AdventurerS1SecretAuctionBid.query
                .outerjoin(
                    ledger,
                    (ledger.c.adventurer_id == AdventurerS1SecretAuctionBid.adventurer_id)
                    & (ledger.c.related_id == cast(AdventurerS1SecretAuctionBid.lot_id, String)),
                )
                .filter(
                    AdventurerS1SecretAuctionBid.status == 'held',
                    func.abs(
                        AdventurerS1SecretAuctionBid.amount + func.coalesce(ledger.c.net, 0),
                    ) >= 0.001,
                )
                .with_entities(
                    AdventurerS1SecretAuctionBid.lot_id,
                    AdventurerS1SecretAuctionBid.adventurer_id,
                )
                .limit(remaining).all()
            )
            candidates.update((row.lot_id, row.adventurer_id) for row in dirty_held)

        result = {'checked': 0, 'invalidated': 0, 'reclaimed': 0.0, 'errors': 0}
        for lot_id, adventurer_id in candidates:
            data, err = S1SecretAuctionService._reconcile_account(lot_id, adventurer_id)
            result['checked'] += 1
            if err:
                result['errors'] += 1
                logger.warning(
                    '拍卖脏数据对账失败 lot=%s adventurer=%s err=%s',
                    lot_id, adventurer_id, err,
                )
                continue
            result['invalidated'] += int(data['invalidated'])
            result['reclaimed'] = round(result['reclaimed'] + float(data['reclaimed']), 2)
        if result['invalidated'] or result['reclaimed'] or result['errors']:
            logger.warning('隐秘拍卖自动对账结果: %s', result)
        return result

    @staticmethod
    def _total_bid_count_map(lot_ids: list[int]) -> dict[int, int]:
        """历史拍品：统计全部出价人数（含已结算）。"""
        if not lot_ids:
            return {}
        rows = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.related_id.in_([str(i) for i in lot_ids]),
                AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                AdventurerS1CurrencyLog.source_type == 'secret_auction_bid',
                AdventurerS1CurrencyLog.change_amount < 0,
            )
            .with_entities(
                AdventurerS1CurrencyLog.related_id,
                func.count(func.distinct(AdventurerS1CurrencyLog.adventurer_id)),
            )
            .group_by(AdventurerS1CurrencyLog.related_id)
            .all()
        )
        return {int(lot_id): int(cnt) for lot_id, cnt in rows}

    @staticmethod
    def _winner_name_map(lot_rows: list) -> dict[int, str]:
        winner_ids = {lot.winner_adventurer_id for lot in lot_rows if lot.winner_adventurer_id}
        if not winner_ids:
            return {}
        return dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(winner_ids)).all()
        )

    @staticmethod
    def _my_result_for_lot(lot, adventurer_id: int, my_bids: dict) -> Optional[str]:
        if not adventurer_id:
            return None
        bid = my_bids.get(lot.id)
        if not bid:
            return None
        if lot.status == 'settled_won' and lot.winner_adventurer_id == adventurer_id:
            return 'won'
        return 'lost'

    @staticmethod
    def _my_bid_map(lot_ids: list[int], adventurer_id: int) -> dict[int, AdventurerS1SecretAuctionBid]:
        if not lot_ids or not adventurer_id:
            return {}
        rows = AdventurerS1SecretAuctionBid.query.filter(
            AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids),
            AdventurerS1SecretAuctionBid.adventurer_id == adventurer_id,
            AdventurerS1SecretAuctionBid.status == 'held',
        ).all()
        return {b.lot_id: b for b in S1SecretAuctionService._funded_held_bids(rows)}

    @staticmethod
    def _history_bid_map(lot_ids: list[int], adventurer_id: int) -> dict[int, AdventurerS1SecretAuctionBid]:
        if not lot_ids or not adventurer_id:
            return {}
        rows = AdventurerS1SecretAuctionBid.query.filter(
            AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids),
            AdventurerS1SecretAuctionBid.adventurer_id == adventurer_id,
        ).all()
        participated_lot_ids = {
            int(lot_id) for (lot_id,) in
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.related_id.in_([str(i) for i in lot_ids]),
                AdventurerS1CurrencyLog.related_type == 'secret_auction_lot',
                AdventurerS1CurrencyLog.currency_code == S1SecretAuctionService.CURRENCY_CODE,
                AdventurerS1CurrencyLog.source_type == 'secret_auction_bid',
                AdventurerS1CurrencyLog.change_amount < 0,
            )
            .with_entities(AdventurerS1CurrencyLog.related_id)
            .distinct().all()
        }
        return {b.lot_id: b for b in rows if b.lot_id in participated_lot_ids}

    @staticmethod
    def _parse_reserve_price(raw, label: str = '') -> Tuple[Optional[float], Optional[str]]:
        if raw is None or raw == '':
            return 0.0, None
        validated, err = S1CurrencyService._validate_amount(raw)
        if err:
            return None, f'{label}底价{err}'
        if validated < 0:
            return None, f'{label}底价不能为负数'
        return validated, None

    @staticmethod
    def _lot_reserve_price(lot) -> float:
        return round(float(getattr(lot, 'reserve_price', 0) or 0), 2)

    @staticmethod
    def _refund_bid(
        bid: AdventurerS1SecretAuctionBid,
        lot_id: int,
        reason: str,
        escrow_amount: Optional[float] = None,
    ) -> Optional[str]:
        if bid.status != 'held':
            return None
        escrow = escrow_amount
        if escrow is None:
            escrow = S1SecretAuctionService._escrow_amount_map([bid]).get(
                (bid.lot_id, bid.adventurer_id), 0.0,
            )
        if escrow <= 0:
            logger.warning(
                '拒绝无托管退款: bid=%s adventurer=%s lot=%s amount=%s',
                bid.id, bid.adventurer_id, lot_id, bid.amount,
            )
            bid.status = 'refunded'
            return None
        _, err = S1CurrencyService.add(
            bid.adventurer_id,
            S1SecretAuctionService.CURRENCY_CODE,
            escrow,
            source_type='secret_auction_refund',
            related_id=lot_id,
            related_type='secret_auction_lot',
            description=reason,
            auto_commit=False,
        )
        if err:
            db_manager.rollback()
            return err
        bid.status = 'refunded'
        return None

    @staticmethod
    def cancel_held_bids_for_adventurer(adventurer_id: int) -> int:
        """取消死亡冒险者的全部托管出价；调用方负责事务提交。"""
        locked_adv = S1SecretAuctionService._lock_adventurers([adventurer_id])
        if int(adventurer_id) not in locked_adv:
            return 0
        bids = (
            AdventurerS1SecretAuctionBid.query
            .filter_by(adventurer_id=adventurer_id, status='held')
            .order_by(AdventurerS1SecretAuctionBid.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        if not bids:
            return 0

        escrow_map = S1SecretAuctionService._escrow_amount_map(bids)
        refunded = 0
        for bid in bids:
            err = S1SecretAuctionService._refund_bid(
                bid,
                bid.lot_id,
                '冒险者死亡，隐秘拍卖出价自动取消退还',
                escrow_amount=escrow_map.get((bid.lot_id, bid.adventurer_id), 0.0),
            )
            if err:
                raise RuntimeError(err)
            refunded += 1
        return refunded

    @staticmethod
    def _parse_lot_uses_left(
        raw: dict, catalog, item_type: str, prefix: str,
    ) -> Tuple[Optional[int], Optional[str]]:
        if raw.get('uses_left') is None or raw.get('uses_left') == '':
            return _default_uses_left(
                catalog.name, item_type or '', getattr(catalog, 'use_condition', None),
            ), None
        try:
            uses_left = int(raw['uses_left'])
        except (TypeError, ValueError):
            return None, f'{prefix}使用次数无效'
        if uses_left < 0:
            return None, f'{prefix}使用次数不能为负'
        return uses_left, None

    @staticmethod
    def _grant_lot_item(adv: AdventurerS1, lot: AdventurerS1SecretAuctionLot) -> None:
        qty = coerce_item_quantity(lot.quantity, 1)
        _, err = grant_raw_item(
            adv,
            lot.item_name,
            {
                'type': lot.item_type or '',
                'rarity': lot.rarity or 'common',
                'quantity': qty,
                'uses_left': lot.uses_left,
                'desc': lot.custom_desc or '',
            },
            source_type='secret_auction_win',
            description=f'隐秘拍卖中标「{lot.item_name}」',
            force_destination='warehouse',
            world_unique_escrow_lot_id=lot.id,
        )
        if err:
            raise RuntimeError(err or '发放拍品失败')
        if lot.custom_desc or lot.uses_left is not None:
            wh = (
                AdventurerS1WarehouseItem.query
                .filter_by(adventurer_id=adv.id, name=lot.item_name)
                .order_by(AdventurerS1WarehouseItem.id.desc())
                .first()
            )
            if wh:
                if lot.custom_desc and not wh.custom_desc:
                    wh.custom_desc = lot.custom_desc
                if lot.uses_left is not None:
                    wh.uses_left = lot.uses_left

    @staticmethod
    def _emit_changed(lot_id: int, action: str) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_s1_secret_auction_changed', {
                'action': action,
                'lot_id': lot_id,
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def _resolve_catalog_for_lot(
        name: str, raw: dict, prefix: str,
    ) -> Tuple[Optional[tuple], Optional[str]]:
        """已有图鉴则沿用；自定义名称则写入 adventurer_s1_items。"""
        from plugins.adventurer_s1.services.s1_item_use_condition_service import normalize_use_condition

        catalog = (
            AdventurerS1Item.query
            .filter_by(name=name)
            .populate_existing()
            .with_for_update()
            .first()
        )
        item_type = (raw.get('item_type') or '').strip()[:50]
        rarity = (raw.get('rarity') or '').strip()
        custom_desc = (raw.get('custom_desc') or raw.get('desc') or '').strip()

        if catalog:
            # 已有图鉴：如果传了 use_condition 且图鉴原来没有，则补上
            raw_uc = raw.get('use_condition')
            if raw_uc and not catalog.use_condition:
                catalog.use_condition = normalize_use_condition(raw_uc)
            resolved_type = catalog.type or 'special'
            resolved_rarity = catalog.rarity or 'common'
            if resolved_rarity not in S1SecretAuctionService.VALID_RARITIES:
                return None, f'{prefix}图鉴稀有度无效'
            desc = custom_desc or catalog.description or None
            return (catalog, resolved_type, resolved_rarity, desc), None

        resolved_type = item_type or 'special'
        resolved_rarity = rarity or 'common'
        if resolved_type not in S1_ITEM_TYPE_SET:
            return None, f'{prefix}物品类型无效'
        if resolved_rarity not in S1SecretAuctionService.VALID_RARITIES:
            return None, f'{prefix}稀有度无效'

        from plugins.adventurer_s1.services.s1_item_world_unique_service import (
            infer_world_unique,
        )

        catalog = AdventurerS1Item(
            name=name,
            type=resolved_type,
            description=custom_desc or '',
            rarity=resolved_rarity,
            is_starter=False,
            world_unique=infer_world_unique(name, resolved_type, custom_desc),
            use_condition=normalize_use_condition(raw.get('use_condition')),
        )
        ok, add_err = db_manager.add(catalog)
        if not ok:
            return None, add_err or f'{prefix}写入图鉴失败'
        return (catalog, resolved_type, resolved_rarity, custom_desc or None), None

    # ── 查询 ────────────────────────────────────────────────────────────────

    @staticmethod
    def list_lots(
        page: int,
        per_page: int,
        user_id: int,
        status: str = 'open',
        adventurer_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """玩家拍品列表（不含他人出价）。"""
        if status and status not in S1SecretAuctionService.VALID_STATUSES:
            return None, '无效的状态'

        viewer_adv_id, err = S1SecretAuctionService._resolve_owned_adventurer_id(user_id, adventurer_id)
        if err:
            return None, err

        query = AdventurerS1SecretAuctionLot.query.filter_by(status='open')
        total = query.count()
        rows = (
            query.order_by(AdventurerS1SecretAuctionLot.ends_at.asc())
            .offset((page - 1) * per_page).limit(per_page).all()
        )
        lot_ids = [r.id for r in rows]
        count_map = S1SecretAuctionService._bid_count_map(lot_ids)
        my_map = S1SecretAuctionService._my_bid_map(lot_ids, viewer_adv_id) if viewer_adv_id else {}
        immutable_names = catalog_immutable_item_names({lot.item_name for lot in rows})

        items = []
        for lot in rows:
            my_bid = my_map.get(lot.id)
            item = lot.to_public_dict(
                bid_count=count_map.get(lot.id, 0),
                my_bid_amount=my_bid.amount if my_bid else None,
            )
            item['is_immutable'] = lot.item_name in immutable_names
            _sanitize_player_lot_item(item, lot)
            items.append(item)
        return build_paginated_response(items, total, page, per_page), None

    @staticmethod
    def open_lot_summary() -> Tuple[Optional[dict], Optional[str]]:
        """建筑分区入口：进行中拍品数量（无需进入拍卖行）。"""
        cached = _open_lot_summary_cache.get('summary')
        if cached is not None:
            return cached, None
        built_err = S1SecretAuctionService._require_auction_built()
        if built_err:
            result = {'available': False, 'open_count': 0}
        else:
            open_count = AdventurerS1SecretAuctionLot.query.filter_by(status='open').count()
            result = {'available': True, 'open_count': int(open_count or 0)}
        _open_lot_summary_cache.set('summary', result)
        return result, None

    @staticmethod
    def list_history_lots(
        page: int,
        per_page: int,
        user_id: int,
        adventurer_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """已结束拍品：展示中标者名称与本人结果，不含任何价格。"""
        viewer_adv_id, err = S1SecretAuctionService._resolve_owned_adventurer_id(user_id, adventurer_id)
        if err:
            return None, err

        query = AdventurerS1SecretAuctionLot.query.filter(
            AdventurerS1SecretAuctionLot.status.in_(S1SecretAuctionService.HISTORY_STATUSES),
        )
        total = query.count()
        rows = (
            query.order_by(AdventurerS1SecretAuctionLot.ends_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all()
        )
        lot_ids = [r.id for r in rows]
        count_map = S1SecretAuctionService._total_bid_count_map(lot_ids)
        name_map = S1SecretAuctionService._winner_name_map(rows)
        my_map = S1SecretAuctionService._history_bid_map(lot_ids, viewer_adv_id) if viewer_adv_id else {}
        immutable_names = catalog_immutable_item_names({lot.item_name for lot in rows})

        items = []
        for lot in rows:
            winner_name = name_map.get(lot.winner_adventurer_id) if lot.winner_adventurer_id else None
            item = lot.to_history_dict(
                bid_count=count_map.get(lot.id, 0),
                winner_name=winner_name,
                my_result=S1SecretAuctionService._my_result_for_lot(lot, viewer_adv_id, my_map),
            )
            item['is_immutable'] = lot.item_name in immutable_names
            _sanitize_player_lot_item(item, lot)
            items.append(item)
        return build_paginated_response(items, total, page, per_page), None

    @staticmethod
    def admin_list_lots(
        page: int,
        per_page: int,
        status: str = None,
        include_live_prices: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员拍品列表；可对进行中拍品隐藏密封出价金额。"""
        query = AdventurerS1SecretAuctionLot.query
        if status:
            if status not in S1SecretAuctionService.VALID_STATUSES:
                return None, '无效的状态'
            query = query.filter_by(status=status)
        total = query.count()
        rows = (
            query.order_by(AdventurerS1SecretAuctionLot.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all()
        )
        lot_ids = [r.id for r in rows]
        open_ids = [r.id for r in rows if r.status == 'open']
        count_map = S1SecretAuctionService._bid_count_map(open_ids)
        for lot_id in open_ids:
            count_map.setdefault(lot_id, 0)
        total_count_map = S1SecretAuctionService._total_bid_count_map(lot_ids)
        for lot_id, cnt in total_count_map.items():
            if lot_id not in count_map:
                count_map[lot_id] = cnt

        bid_rows = (
            AdventurerS1SecretAuctionBid.query
            .options(joinedload(AdventurerS1SecretAuctionBid.adventurer))
            .filter(AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids))
            .order_by(AdventurerS1SecretAuctionBid.amount.desc())
            .all()
        ) if lot_ids else []
        open_bid_rows = [b for b in bid_rows if b.lot_id in open_ids]
        valid_open_ids = {b.id for b in S1SecretAuctionService._funded_held_bids(open_bid_rows)}
        bid_rows = [
            b for b in bid_rows
            if b.lot_id not in open_ids or b.id in valid_open_ids
        ]
        bids_by_lot: dict[int, list] = {}
        for b in bid_rows:
            bids_by_lot.setdefault(b.lot_id, []).append(b)

        items = []
        for lot in rows:
            lot_bids = bids_by_lot.get(lot.id, [])
            if not include_live_prices and lot.status not in S1SecretAuctionService.HISTORY_STATUSES:
                item = lot.to_admin_dict(
                    bid_count=count_map.get(lot.id, 0),
                )
                item.pop('winning_amount', None)
                readonly_bids = []
                for bid in sorted(
                    lot_bids,
                    key=lambda row: (row.created_at or datetime.min, row.id or 0),
                ):
                    bid_data = bid.to_admin_dict()
                    readonly_bids.append({
                        field: bid_data[field]
                        for field in S1SecretAuctionService.READONLY_OPEN_BID_FIELDS
                        if field in bid_data
                    })
                item['bids'] = readonly_bids
                item['bid_amounts_hidden'] = True
            else:
                item = lot.to_admin_dict(
                    bid_count=count_map.get(lot.id, 0),
                    bids=lot_bids,
                )
            items.append(item)
        return build_paginated_response(items, total, page, per_page), None

    # ── 管理员操作 ──────────────────────────────────────────────────────────

    @staticmethod
    @with_transaction(auto_commit=True)
    def admin_create_lots(
        user_id: int, lots: list, default_ends_at=None, default_reserve_price=None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """批量上架拍品。lots: [{item_name, ..., reserve_price?}]"""
        if not lots or not isinstance(lots, list):
            return None, '请提供至少一个拍品'

        default_dt, err = S1SecretAuctionService._parse_ends_at(default_ends_at)
        if default_ends_at and err:
            return None, err

        default_reserve, err = S1SecretAuctionService._parse_reserve_price(default_reserve_price)
        if err:
            return None, err

        now = _portable_utc_now()
        created = []
        requested_names = []
        for idx, raw in enumerate(lots):
            if not isinstance(raw, dict):
                return None, f'第 {idx + 1} 个拍品格式无效'
            name = (raw.get('item_name') or raw.get('name') or '').strip()
            if not name:
                return None, f'第 {idx + 1} 个拍品缺少名称'
            if len(name) > 100:
                return None, f'第 {idx + 1} 个拍品名称过长'
            requested_names.append(name)
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            lock_catalog_names,
        )

        lock_catalog_names(
            requested_names,
            shared=False,
            wait=True,
        )
        for idx, raw in enumerate(lots):
            prefix = f'第 {idx + 1} 个拍品'
            name = (raw.get('item_name') or raw.get('name') or '').strip()

            resolved, err = S1SecretAuctionService._resolve_catalog_for_lot(name, raw, prefix)
            if err:
                db_manager.rollback()
                return None, err
            catalog, item_type, rarity, custom_desc = resolved
            from plugins.adventurer_s1.services.s1_item_world_unique_service import (
                load_world_unique_occupancy,
            )
            from plugins.adventurer_s1.s1_item_types import (
                companion_allows_duplicate_name_instances,
            )

            lot_quantity = coerce_item_quantity(raw.get('quantity', 1), 1)
            is_world_unique = bool(catalog.world_unique)
            if is_world_unique:
                if (
                    not companion_allows_duplicate_name_instances(item_type or '')
                    and lot_quantity != 1
                ):
                    db_manager.rollback()
                    return None, f'{prefix}为全服唯一，数量必须为 1'
                occupancy = load_world_unique_occupancy([catalog.name]).get(
                    catalog.name,
                    {'holders': set(), 'escrowed': False},
                )
                if occupancy['holders'] or occupancy['escrowed']:
                    db_manager.rollback()
                    return None, f'{prefix}为全服唯一，当前已被持有或托管'
            ends_raw = raw.get('ends_at') or default_ends_at
            ends_at, err = S1SecretAuctionService._parse_ends_at(ends_raw)
            if err:
                db_manager.rollback()
                return None, f'{prefix}：{err}'
            if ends_at <= now:
                db_manager.rollback()
                return None, f'{prefix}开奖时间须在未来'

            reserve_raw = raw.get('reserve_price') if raw.get('reserve_price') is not None else default_reserve_price
            reserve_price, err = S1SecretAuctionService._parse_reserve_price(reserve_raw, label=prefix)
            if err:
                db_manager.rollback()
                return None, err

            uses_left, err = S1SecretAuctionService._parse_lot_uses_left(
                raw, catalog, item_type or '', prefix,
            )
            if err:
                db_manager.rollback()
                return None, err

            lot = AdventurerS1SecretAuctionLot(
                item_name=catalog.name,
                item_type=item_type,
                custom_desc=custom_desc,
                rarity=rarity,
                quantity=lot_quantity,
                uses_left=uses_left,
                ends_at=ends_at,
                reserve_price=reserve_price,
                status='open',
                created_by_user_id=user_id,
            )
            ok, add_err = db_manager.add(lot)
            if not ok:
                db_manager.rollback()
                return None, add_err or '创建拍品失败'
            created.append(lot)

        S1SecretAuctionService._after_open_lot_mutation()
        return {
            'created_count': len(created),
            'lots': [lot.to_public_dict(bid_count=0) for lot in created],
        }, None

    @staticmethod
    def _after_open_lot_mutation() -> None:
        invalidate_open_lot_summary_cache()
        try:
            from plugins.adventurer_s1.services.adventurer_s1_home_service import (
                invalidate_home_auction_badges_cache,
            )
            invalidate_home_auction_badges_cache()
        except Exception:
            pass

    @staticmethod
    @with_transaction(auto_commit=True)
    def admin_cancel_lot(lot_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员取消未结算拍品，退还所有出价。"""
        lot = AdventurerS1SecretAuctionLot.query.filter_by(id=lot_id).with_for_update().first()
        if not lot:
            return None, '拍品不存在'
        if lot.status != 'open':
            return None, '只能取消进行中的拍品'

        _adventurer_map, bids = S1SecretAuctionService._lock_held_bids(lot.id)
        for bid in bids:
            err = S1SecretAuctionService._refund_bid(
                bid,
                lot.id,
                f'拍卖取消退还（{lot.item_name}）',
            )
            if err:
                return None, err

        lot.status = 'cancelled'
        S1SecretAuctionService._after_open_lot_mutation()
        return {'lot_id': lot.id, 'status': lot.status, 'refunded_bids': len(bids)}, None

    # ── 玩家出价 ────────────────────────────────────────────────────────────

    @staticmethod
    @with_transaction(auto_commit=True)
    def place_bid(
        user_id: int,
        adventurer_id: int,
        lot_id: int,
        amount,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """密封出价：预先扣金币，可改价（退旧扣新）。"""
        built_err = S1SecretAuctionService._require_auction_built()
        if built_err:
            return None, built_err

        adv, err = S1SecretAuctionService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        validated, err = S1CurrencyService._validate_amount(amount)
        if err:
            return None, err
        amount = validated
        if amount <= 0:
            return None, '出价须大于 0'

        lot = (
            AdventurerS1SecretAuctionLot.query
            .filter_by(id=lot_id, status='open')
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not lot:
            return None, '拍品不存在或已结束'

        locked_adv = S1SecretAuctionService._lock_adventurers([adv.id]).get(adv.id)
        if not locked_adv or locked_adv.owner_id != user_id:
            return None, '无权操作此冒险者'
        adv = locked_adv
        can_dispatch, dispatch_reason = adv.can_dispatch()
        if not can_dispatch:
            return None, dispatch_reason or '当前状态无法参与拍卖'
        if lot.ends_at <= _portable_utc_now():
            return None, '拍品已截止，无法出价'
        reserve = S1SecretAuctionService._lot_reserve_price(lot)
        if reserve > 0 and amount < reserve:
            return None, f'出价须不低于底价 {reserve}💰'

        existing = (
            AdventurerS1SecretAuctionBid.query
            .filter_by(lot_id=lot.id, adventurer_id=adv.id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        existing_is_funded = bool(
            existing
            and S1SecretAuctionService._funded_held_bids([existing])
        )
        if existing and existing.status == 'held' and existing_is_funded:
            # 改价：先扣差额，成功后再更新 bid；失败则什么都不变
            old_amount = round(float(existing.amount), 2)
            delta = round(amount - old_amount, 2)
            if delta > 0:
                _, deduct_err = S1CurrencyService.deduct(
                    adv.id,
                    S1SecretAuctionService.CURRENCY_CODE,
                    delta,
                    source_type='secret_auction_bid',
                    related_id=lot.id,
                    related_type='secret_auction_lot',
                    description=f'隐秘拍卖改价加扣（{lot.item_name}）',
                    auto_commit=False,
                )
                if deduct_err:
                    db_manager.rollback()
                    return None, deduct_err
            elif delta < 0:
                _, refund_err = S1CurrencyService.add(
                    adv.id,
                    S1SecretAuctionService.CURRENCY_CODE,
                    -delta,
                    source_type='secret_auction_refund',
                    related_id=lot.id,
                    related_type='secret_auction_lot',
                    description=f'隐秘拍卖改价退还（{lot.item_name}）',
                    auto_commit=False,
                )
                if refund_err:
                    db_manager.rollback()
                    return None, refund_err
            existing.amount = amount
            existing.status = 'held'
            bid = existing
        else:
            # 新出价（或旧 bid 已 refunded/cancelled）：先扣金币，再创建/复用行
            _, deduct_err = S1CurrencyService.deduct(
                adv.id,
                S1SecretAuctionService.CURRENCY_CODE,
                amount,
                source_type='secret_auction_bid',
                related_id=lot.id,
                related_type='secret_auction_lot',
                description=f'隐秘拍卖出价（{lot.item_name}）',
                auto_commit=False,
            )
            if deduct_err:
                db_manager.rollback()
                return None, deduct_err
            if existing:
                # 复用已有的 refunded/cancelled 行
                existing.amount = amount
                existing.status = 'held'
                bid = existing
            else:
                bid = AdventurerS1SecretAuctionBid(
                    lot_id=lot.id,
                    adventurer_id=adv.id,
                    amount=amount,
                    status='held',
                )
                ok, add_err = db_manager.add(bid)
                if not ok:
                    db_manager.rollback()
                    return None, add_err or '记录出价失败'

        count_map = S1SecretAuctionService._bid_count_map([lot.id])
        return {
            'lot_id': lot.id,
            'my_bid_amount': amount,
            'bid_count': count_map.get(lot.id, 0),
        }, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def cancel_bid(
        user_id: int,
        adventurer_id: int,
        lot_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """取消出价：退还金币，标记 bid 为 refunded。"""
        built_err = S1SecretAuctionService._require_auction_built()
        if built_err:
            return None, built_err

        adv, err = S1SecretAuctionService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        lot = (
            AdventurerS1SecretAuctionLot.query
            .filter_by(id=lot_id, status='open')
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not lot:
            return None, '拍品不存在或已结束'

        locked_adv = S1SecretAuctionService._lock_adventurers([adv.id]).get(adv.id)
        if not locked_adv or locked_adv.owner_id != user_id:
            return None, '无权操作此冒险者'

        bid = (
            AdventurerS1SecretAuctionBid.query
            .filter_by(lot_id=lot.id, adventurer_id=adv.id, status='held')
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not bid:
            return None, '你没有对此拍品的出价'

        refund_err = S1SecretAuctionService._refund_bid(
            bid, lot.id, f'取消出价退还（{lot.item_name}）',
        )
        if refund_err:
            db_manager.rollback()
            return None, refund_err

        count_map = S1SecretAuctionService._bid_count_map([lot.id])
        return {
            'lot_id': lot.id,
            'bid_count': count_map.get(lot.id, 0),
        }, None

    # ── 结算 ────────────────────────────────────────────────────────────────

    @staticmethod
    def settle_due_lots() -> int:
        """扫描到期拍品并结算。返回本次处理条数。"""
        now = _portable_utc_now()
        due_ids = [
            row.id for row in
            AdventurerS1SecretAuctionLot.query
            .filter(
                AdventurerS1SecretAuctionLot.status == 'open',
                AdventurerS1SecretAuctionLot.ends_at <= now,
            )
            .with_entities(AdventurerS1SecretAuctionLot.id)
            .limit(50)
            .all()
        ]
        settled = 0
        for lot_id in due_ids:
            try:
                ok, err = S1SecretAuctionService._settle_one_lot(lot_id)
            except ConcurrencyError as exc:
                logger.warning('隐秘拍卖结算遇到并发冲突 lot=%s err=%s', lot_id, exc)
                continue
            if err:
                logger.warning('隐秘拍卖结算失败 lot=%s err=%s', lot_id, err)
            elif ok:
                settled += 1
                lot_status = AdventurerS1SecretAuctionLot.query.with_entities(
                    AdventurerS1SecretAuctionLot.status,
                ).filter_by(id=lot_id).scalar()
                S1SecretAuctionService._emit_changed(lot_id, lot_status or 'settled')
        if settled > 0:
            S1SecretAuctionService._after_open_lot_mutation()
        return settled

    @staticmethod
    @db_retry(max_retries=3)
    @with_transaction(auto_commit=True)
    def _settle_one_lot(lot_id: int) -> Tuple[bool, Optional[str]]:
        lot = (
            AdventurerS1SecretAuctionLot.query
            .filter_by(id=lot_id, status='open')
            .with_for_update()
            .first()
        )
        if not lot:
            return False, None

        bidder_map, all_held_bids = S1SecretAuctionService._lock_held_bids(
            lot.id,
            lock_warehouses=True,
        )
        bids = S1SecretAuctionService._funded_held_bids(all_held_bids)
        bidder_statuses = {row.id: row.status for row in bidder_map.values()}
        ledger_item_name = lot.item_name
        dead_bids = [bid for bid in bids if bidder_statuses.get(bid.adventurer_id) == 'dead']
        for bid in dead_bids:
            err = S1SecretAuctionService._refund_bid(
                bid, lot.id, f'冒险者死亡，出价自动取消退还（{ledger_item_name}）',
            )
            if err:
                return False, err
        dead_bid_ids = {bid.id for bid in dead_bids}
        bids = [bid for bid in bids if bid.id not in dead_bid_ids]
        funded_ids = {b.id for b in bids}
        unfunded_held = [b for b in all_held_bids if b.id not in funded_ids]
        if not bids:
            # 无有效出价，清理因改价导致 escrow 归零的残留 held bid
            for bid in unfunded_held:
                bid.status = 'refunded'
            lot.status = 'settled_pass'
            return True, None

        max_amount = max(b.amount for b in bids)
        top_bids = [b for b in bids if round(float(b.amount), 2) == round(float(max_amount), 2)]

        if len(top_bids) > 1:
            for bid in bids:
                err = S1SecretAuctionService._refund_bid(
                    bid, lot.id, f'同价流拍退还（{ledger_item_name}）',
                )
                if err:
                    return False, err
            for bid in unfunded_held:
                bid.status = 'refunded'
            lot.status = 'settled_pass'
            return True, None

        reserve = S1SecretAuctionService._lot_reserve_price(lot)
        if reserve > 0 and round(float(max_amount), 2) < reserve:
            for bid in bids:
                err = S1SecretAuctionService._refund_bid(
                    bid, lot.id, f'未达底价流拍退还（{ledger_item_name}）',
                )
                if err:
                    return False, err
            for bid in unfunded_held:
                bid.status = 'refunded'
            lot.status = 'settled_pass'
            return True, None

        winner_bid = top_bids[0]
        winner_adv = bidder_map.get(winner_bid.adventurer_id)
        if not winner_adv:
            for bid in bids:
                err = S1SecretAuctionService._refund_bid(
                    bid,
                    lot.id,
                    f'中标者异常流拍退还（{ledger_item_name}）',
                )
                if err:
                    return False, err
            for bid in unfunded_held:
                bid.status = 'refunded'
            lot.status = 'settled_pass'
            return True, None

        try:
            S1SecretAuctionService._grant_lot_item(winner_adv, lot)
        except RuntimeError as e:
            return False, str(e)

        winner_bid.status = 'won'
        for bid in bids:
            if bid.id != winner_bid.id:
                err = S1SecretAuctionService._refund_bid(
                    bid, lot.id, f'未中标退还（{ledger_item_name}）',
                )
                if err:
                    return False, err

        lot.status = 'settled_won'
        lot.winner_adventurer_id = winner_bid.adventurer_id
        lot.winning_amount = round(float(winner_bid.amount), 2)

        # 清理因改价导致 escrow 归零的残留 held bid
        for bid in unfunded_held:
            bid.status = 'refunded'

        return True, None

    # ── 改价归零修复 ───────────────────────────────────────────────────────

    @staticmethod
    def preview_unfunded_held_fix() -> Tuple[Optional[dict], Optional[str]]:
        """预览：找所有已结算拍品中 status=held 但 escrow=0 的残留 bid。"""
        settled_lots = (
            AdventurerS1SecretAuctionLot.query
            .filter(AdventurerS1SecretAuctionLot.status.in_(['settled_won', 'settled_pass']))
            .with_entities(AdventurerS1SecretAuctionLot.id)
            .all()
        )
        lot_ids = [row.id for row in settled_lots]
        if not lot_ids:
            return {'affected': [], 'total_gold': 0}, None

        held_bids = (
            AdventurerS1SecretAuctionBid.query
            .filter(
                AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids),
                AdventurerS1SecretAuctionBid.status == 'held',
            )
            .all()
        )
        if not held_bids:
            return {'affected': [], 'total_gold': 0}, None

        escrow = S1SecretAuctionService._escrow_amount_map(held_bids)
        affected = []
        for bid in held_bids:
            e = escrow.get((bid.lot_id, bid.adventurer_id), 0.0)
            if e <= 0:
                adv = AdventurerS1.query.session.get(AdventurerS1, bid.adventurer_id)
                lot = AdventurerS1SecretAuctionLot.query.session.get(AdventurerS1SecretAuctionLot, bid.lot_id)
                affected.append({
                    'lot_id': bid.lot_id,
                    'item_name': lot.item_name if lot else '?',
                    'adventurer_id': bid.adventurer_id,
                    'adventurer_name': adv.name if adv else '?',
                    'bid_amount': round(float(bid.amount), 2),
                })
        return {'affected': affected, 'total_gold': round(sum(a['bid_amount'] for a in affected), 2)}, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def apply_unfunded_held_fix() -> Tuple[Optional[dict], Optional[str]]:
        """执行：将已结算拍品中 status=held 且 escrow=0 的 bid 标记为 refunded。"""
        settled_lots = (
            AdventurerS1SecretAuctionLot.query
            .filter(AdventurerS1SecretAuctionLot.status.in_(['settled_won', 'settled_pass']))
            .with_entities(AdventurerS1SecretAuctionLot.id)
            .all()
        )
        lot_ids = [row.id for row in settled_lots]
        if not lot_ids:
            return {'fixed': 0, 'total_gold': 0}, None

        held_bids = (
            AdventurerS1SecretAuctionBid.query
            .filter(
                AdventurerS1SecretAuctionBid.lot_id.in_(lot_ids),
                AdventurerS1SecretAuctionBid.status == 'held',
            )
            .with_for_update()
            .all()
        )
        if not held_bids:
            return {'fixed': 0, 'total_gold': 0}, None

        escrow = S1SecretAuctionService._escrow_amount_map(held_bids)
        fixed = 0
        total_gold = 0.0
        for bid in held_bids:
            e = escrow.get((bid.lot_id, bid.adventurer_id), 0.0)
            if e <= 0:
                bid.status = 'refunded'
                fixed += 1
                total_gold += round(float(bid.amount), 2)
        return {'fixed': fixed, 'total_gold': round(total_gold, 2)}, None
