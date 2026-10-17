"""深渊酒馆 S1 个人交易市场。

规则（2026-06-12 拍板，2026-06-13 卖方预扣，2026-06-17 卖钱模式）：
- 玩家把绑定冒险者**仓库**里的物品挂上市场（物品移出仓库、快照托管在挂单行）
- 两种模式：
  - barter（以物易物）：同稀有度互换，卖方手续费挂单时预扣，买方手续费成交时扣
  - sell（卖钱）：卖方标价（在物价范围内），买方用金币购买，成交时收 20% 卖方手续费
- 草药店类消耗品禁止上架/出价；带 uses_left 的物品可交易（列表展示剩余次数）
"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import (
    S1PlayerVisibleError, player_database_result, player_operation_failure,
)
import logging
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import and_, case, exists, func, or_

from db_repository import db_manager, db_retry
from utils.pagination import build_paginated_response
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1Item, AdventurerS1WarehouseItem,
    AdventurerS1MarketListing, AdventurerS1MarketOffer,
    AdventurerS1MarketBuyOrder, AdventurerS1MarketBuyOffer,
    AdventurerS1ItemLog,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of

logger = logging.getLogger(__name__)


class S1MarketService:
    """个人交易市场：挂单 / 取消 / 同稀有度交换 / 卖钱。"""

    VALID_RARITIES = ('common', 'rare', 'epic', 'legendary')

    # 稀有度中文映射（description 展示用）
    RARITY_LABELS = {
        'common': '普通',
        'rare': '稀有',
        'epic': '史诗',
        'legendary': '传说',
    }

    # 与 plugin.default_config 的 ADVENTURER_S1_MARKET_FEES 同值兜底（测试环境不走 PluginLoader）
    _DEFAULT_FEES = {'common': 2.0, 'rare': 12.0, 'epic': 60.0, 'legendary': 250.0}

    # 卖钱模式价格范围兜底（与 _S1_PRICE_ADDENDUM 物价设定对齐）
    _DEFAULT_SELL_PRICE_RANGE = {
        'common': {'min': 10, 'max': 50},
        'rare': {'min': 60, 'max': 300},
        'epic': {'min': 400, 'max': 1500},
        'legendary': {'min': 2000, 'max': 10000},
    }

    _DEFAULT_SELL_FEE_RATE = 0.2

    # ── 内部工具 ────────────────────────────────────────────────────────────

    @staticmethod
    def _get_fees() -> dict:
        fees = current_app.config.get('ADVENTURER_S1_MARKET_FEES') or S1MarketService._DEFAULT_FEES
        return {
            r: round(float(fees.get(r, S1MarketService._DEFAULT_FEES[r])), 2)
            for r in S1MarketService.VALID_RARITIES
        }

    @staticmethod
    def _fee_for(rarity: str) -> float:
        return S1MarketService._get_fees().get(rarity, 0.0)

    @staticmethod
    def _get_sell_price_range() -> dict:
        cfg = current_app.config.get('ADVENTURER_S1_MARKET_SELL_PRICE_RANGE')
        if isinstance(cfg, dict) and cfg:
            return cfg
        return S1MarketService._DEFAULT_SELL_PRICE_RANGE

    @staticmethod
    def _get_sell_fee_rate() -> float:
        try:
            return float(current_app.config.get('ADVENTURER_S1_MARKET_SELL_FEE_RATE', S1MarketService._DEFAULT_SELL_FEE_RATE))
        except (TypeError, ValueError):
            return S1MarketService._DEFAULT_SELL_FEE_RATE

    @staticmethod
    def _clue_catalog_rarity_map(item_names) -> dict[str, str]:
        """批量读取追查线索的目录当前稀有度；普通物品不进入覆盖表。"""
        names = {
            str(name or '').strip()
            for name in (item_names or [])
            if str(name or '').strip()
        }
        if not names:
            return {}
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            is_hidden_boss_clue_item,
        )

        rows = (
            AdventurerS1Item.query
            .filter(AdventurerS1Item.name.in_(sorted(names)))
            .all()
        )
        return {
            row.name: str(row.rarity or 'common')
            for row in rows
            if is_hidden_boss_clue_item(row.use_condition)
        }

    @staticmethod
    def _effective_item_rarity(
        item_name: str,
        fallback: str,
        clue_rarity_map: Optional[dict[str, str]] = None,
    ) -> str:
        rarity_map = (
            clue_rarity_map
            if clue_rarity_map is not None
            else S1MarketService._clue_catalog_rarity_map({item_name})
        )
        return str(rarity_map.get(item_name) or fallback or 'common')

    @staticmethod
    def sync_hidden_boss_clue_market_state(rarity_by_name: dict[str, str]) -> dict[str, int]:
        """在 Boss 状态事务内批量同步仍在流转中的线索市场快照。"""
        normalized: dict[str, set[str]] = {}
        for raw_name, raw_rarity in (rarity_by_name or {}).items():
            name = str(raw_name or '').strip()
            rarity = str(raw_rarity or '').strip()
            if name and rarity in S1MarketService.VALID_RARITIES:
                normalized.setdefault(rarity, set()).add(name)
        counts = {'market_listings': 0, 'market_offers': 0, 'market_buy_offers': 0}
        if not normalized:
            return counts

        now = _portable_utc_now()
        price_ranges = S1MarketService._get_sell_price_range()
        for rarity, names in normalized.items():
            listing_query = AdventurerS1MarketListing.query.filter(
                AdventurerS1MarketListing.status == 'active',
                AdventurerS1MarketListing.item_name.in_(sorted(names)),
            )
            listing_values = {
                AdventurerS1MarketListing.rarity: rarity,
                AdventurerS1MarketListing.updated_at: now,
            }
            max_unit_price = float((price_ranges.get(rarity) or {}).get('max', 0) or 0)
            if max_unit_price > 0:
                max_total_price = max_unit_price * func.coalesce(
                    AdventurerS1MarketListing.quantity,
                    1,
                )
                overpriced = and_(
                    AdventurerS1MarketListing.listing_type == 'sell',
                    AdventurerS1MarketListing.price.isnot(None),
                    AdventurerS1MarketListing.price > max_total_price,
                )
                listing_query = listing_query.filter(or_(
                    AdventurerS1MarketListing.rarity != rarity,
                    overpriced,
                ))
                listing_values[AdventurerS1MarketListing.price] = case(
                    (overpriced, max_total_price),
                    else_=AdventurerS1MarketListing.price,
                )
            else:
                listing_query = listing_query.filter(
                    AdventurerS1MarketListing.rarity != rarity,
                )
            counts['market_listings'] += int(listing_query.update(
                listing_values,
                synchronize_session=False,
            ))

            counts['market_offers'] += int(
                AdventurerS1MarketOffer.query.filter(
                    AdventurerS1MarketOffer.status == 'pending',
                    AdventurerS1MarketOffer.offered_item_name.in_(sorted(names)),
                    AdventurerS1MarketOffer.offered_rarity != rarity,
                ).update({
                    AdventurerS1MarketOffer.offered_rarity: rarity,
                    AdventurerS1MarketOffer.updated_at: now,
                }, synchronize_session=False)
            )
            counts['market_buy_offers'] += int(
                AdventurerS1MarketBuyOffer.query.filter(
                    AdventurerS1MarketBuyOffer.status == 'pending',
                    AdventurerS1MarketBuyOffer.item_name.in_(sorted(names)),
                    AdventurerS1MarketBuyOffer.rarity != rarity,
                ).update({
                    AdventurerS1MarketBuyOffer.rarity: rarity,
                    AdventurerS1MarketBuyOffer.updated_at: now,
                }, synchronize_session=False)
            )
        return counts

    @staticmethod
    def _finalize_market_client_payload(*payloads) -> None:
        """对返回客户端的市场 dict 应用线索展示脱敏（与仓库/身上一致）。"""
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            sanitize_market_listing_dict,
            sanitize_market_offer_dict,
            sanitize_market_trade_result_dict,
        )

        dicts: list[dict] = []
        for payload in payloads:
            if isinstance(payload, dict):
                dicts.append(payload)
            elif isinstance(payload, list):
                dicts.extend(item for item in payload if isinstance(item, dict))
        if not dicts:
            return

        names: set[str] = set()
        for data in dicts:
            for key in (
                'item_name', 'traded_item_name', 'offered_item_name',
                'received_item', 'given_item',
            ):
                value = data.get(key)
                if value:
                    names.add(str(value))
            listing = data.get('listing')
            if isinstance(listing, dict):
                value = listing.get('item_name')
                if value:
                    names.add(str(value))
        if not names:
            return

        clue_meta = AdventurerS1Service._batch_clue_meta_by_item_names(names)
        if not clue_meta:
            return
        for data in dicts:
            if 'offered_item_name' in data and 'listing_id' in data:
                sanitize_market_offer_dict(data, clue_meta)
            elif 'received_item' in data or 'given_item' in data:
                sanitize_market_trade_result_dict(data, clue_meta)
            else:
                sanitize_market_listing_dict(data, clue_meta)
            if (
                data.get('listing_type') == 'sell'
                and data.get('item_name') in clue_meta
                and data.get('price') is not None
            ):
                data['price'] = S1MarketService._clamp_sell_total_price(
                    str(data.get('rarity') or 'common'),
                    float(data.get('price') or 0),
                    int(data.get('quantity') or 1),
                )

    @staticmethod
    def _clamp_sell_total_price(rarity: str, price: float, quantity: int) -> float:
        """目录降级后只向下压价，不改写原本处于合法区间内的卖家定价。"""
        rng = S1MarketService._get_sell_price_range().get(rarity) or {}
        max_unit = float(rng.get('max', 0) or 0)
        if max_unit <= 0:
            return round(float(price or 0), 2)
        max_total = max_unit * max(1, int(quantity or 1))
        return round(min(float(price or 0), max_total), 2)

    @staticmethod
    def _calc_sell_buy_totals(
        listing_price: float, listing_qty: int, buy_qty: Optional[int] = None,
    ) -> Tuple[int, float, Optional[str]]:
        """按比例计算本次购买数量与应付金币。buy_qty 为 None 表示买尽剩余。"""
        listing_price = round(float(listing_price or 0), 2)
        listing_qty = max(1, int(listing_qty or 1))
        if buy_qty is None:
            return listing_qty, listing_price, None
        try:
            buy_qty = int(buy_qty)
        except (TypeError, ValueError):
            return 0, 0.0, '购买数量格式不正确'
        if buy_qty <= 0:
            return 0, 0.0, '购买数量至少为 1'
        if buy_qty > listing_qty:
            return 0, 0.0, f'该挂单仅剩 {listing_qty} 件'
        if buy_qty >= listing_qty:
            return listing_qty, listing_price, None
        unit = listing_price / listing_qty
        return buy_qty, round(unit * buy_qty, 2), None

    @staticmethod
    def _resolve_barter_trade_qty(
        listing_qty: int,
        requested: Optional[int],
        offered_stack_qty: int,
    ) -> Tuple[int, Optional[str]]:
        """以物易物单次成交数量：换出/换入须同数；requested 为 None 表示换尽挂单剩余。"""
        listing_qty = max(1, int(listing_qty or 1))
        offered_stack_qty = max(1, int(offered_stack_qty or 1))
        if requested is None:
            trade_qty = listing_qty
        else:
            try:
                trade_qty = int(requested)
            except (TypeError, ValueError):
                return 0, '交换数量格式不正确'
            if trade_qty <= 0:
                return 0, '交换数量至少为 1'
            if trade_qty > listing_qty:
                return 0, f'该挂单仅剩 {listing_qty} 件'
        if offered_stack_qty < trade_qty:
            return 0, f'出价物品数量不足（需要 {trade_qty} 件，当前 {offered_stack_qty} 件）'
        return trade_qty, None

    @staticmethod
    def _offer_listing_quantity(offer: AdventurerS1MarketOffer) -> int:
        if offer.listing_quantity is not None:
            return max(1, int(offer.listing_quantity))
        return max(1, int(offer.offered_quantity or 1))

    @staticmethod
    def _barter_offer_snapshot_matches(
        offer: AdventurerS1MarketOffer,
        item: AdventurerS1WarehouseItem,
        *,
        effective_rarity: Optional[str] = None,
    ) -> bool:
        stage_matches = (
            offer.offered_evolution_stage is None
            or max(0, int(offer.offered_evolution_stage)) == max(
                0, int(item.evolution_stage or 0),
            )
        )
        return (
            offer.offered_item_name == item.name
            and (offer.offered_item_type or '') == (item.type or '')
            and offer.offered_rarity == (effective_rarity or item.rarity)
            and (offer.offered_custom_desc or '') == (item.custom_desc or '')
            and offer.offered_uses_left == item.uses_left
            and stage_matches
        )

    @staticmethod
    def _reject_incompatible_pending_offers(
        listing_id: int,
        remaining_qty: int,
        *,
        exclude_offer_id: int | None = None,
    ) -> None:
        """部分成交后，拒绝需要超过剩余库存的 pending 出价。"""
        q = AdventurerS1MarketOffer.query.filter_by(
            listing_id=listing_id, status='pending',
        )
        if exclude_offer_id is not None:
            q = q.filter(AdventurerS1MarketOffer.id != int(exclude_offer_id))
        now = _portable_utc_now()
        for offer in q.all():
            want = S1MarketService._offer_listing_quantity(offer)
            if want > remaining_qty:
                offer.status = 'rejected'
                offer.updated_at = now

    @staticmethod
    def confiscate_dead_adventurer_residuals(adventurer_id: int) -> int:
        """系统没收死亡冒险者的市场残留，仅按 adventurer_id 清理。"""
        now = _portable_utc_now()
        listing_rows = AdventurerS1MarketListing.query.filter(
            AdventurerS1MarketListing.seller_adventurer_id == adventurer_id,
            AdventurerS1MarketListing.status == 'active',
        ).all()
        listing_ids = [row.id for row in listing_rows]

        changed = 0
        if listing_ids:
            logs = [
                AdventurerS1ItemLog(
                    adventurer_id=adventurer_id,
                    item_name=listing.item_name,
                    item_type=listing.item_type,
                    item_rarity=listing.rarity,
                    change_type='consume',
                    quantity=item_quantity_of(listing),
                    source_type='death_confiscate',
                    related_id=listing.id,
                    related_type='market_listing',
                    description='冒险者死亡，系统没收市场托管物品',
                    created_at=now,
                )
                for listing in listing_rows
            ]
            if logs:
                ok, err = db_manager.add_all(logs, flush=False)
                if not ok:
                    raise RuntimeError(err or '写入死亡没收市场物品流水失败')
            changed += AdventurerS1MarketListing.query.filter(
                AdventurerS1MarketListing.id.in_(listing_ids),
                AdventurerS1MarketListing.status == 'active',
            ).update({
                AdventurerS1MarketListing.status: 'cancelled',
                AdventurerS1MarketListing.updated_at: now,
            }, synchronize_session=False)
            AdventurerS1MarketOffer.query.filter(
                AdventurerS1MarketOffer.listing_id.in_(listing_ids),
                AdventurerS1MarketOffer.status == 'pending',
            ).update({
                AdventurerS1MarketOffer.status: 'rejected',
                AdventurerS1MarketOffer.updated_at: now,
            }, synchronize_session=False)

        AdventurerS1MarketOffer.query.filter(
            AdventurerS1MarketOffer.buyer_adventurer_id == adventurer_id,
            AdventurerS1MarketOffer.status == 'pending',
        ).update({
            AdventurerS1MarketOffer.status: 'cancelled',
            AdventurerS1MarketOffer.updated_at: now,
        }, synchronize_session=False)

        changed += S1MarketBuyOrderService.cancel_adventurer_activity(adventurer_id)
        for listing_id in listing_ids:
            S1MarketService._emit_market_changed('confiscated', listing_id)
        return int(changed or 0)

    @staticmethod
    def _execute_barter_trade(
        listing_row: AdventurerS1MarketListing,
        seller_adv: AdventurerS1,
        buyer_adv: AdventurerS1,
        offered_wh_row: AdventurerS1WarehouseItem,
        trade_qty: int,
        listing_id: int,
    ) -> dict:
        """在事务内完成以物易物：扣买方出价堆、按 trade_qty 交付双方物品、更新挂单剩余。"""
        from plugins.adventurer_s1.services.s1_item_instance_service import take_from_stack

        trade_qty = max(1, int(trade_qty))
        listing_qty = item_quantity_of(listing_row)
        if trade_qty > listing_qty:
            raise S1PlayerVisibleError(f'该挂单仅剩 {listing_qty} 件')
        if item_quantity_of(offered_wh_row) < trade_qty:
            raise S1PlayerVisibleError('出价物品数量不足')

        err = S1MarketService._check_tradeable_item(offered_wh_row)
        if err:
            raise S1PlayerVisibleError(err)
        clue_rarity_map = S1MarketService._clue_catalog_rarity_map({
            listing_row.item_name,
            offered_wh_row.name,
        })
        listing_rarity = S1MarketService._effective_item_rarity(
            listing_row.item_name, listing_row.rarity, clue_rarity_map,
        )
        offered_rarity = S1MarketService._effective_item_rarity(
            offered_wh_row.name, offered_wh_row.rarity, clue_rarity_map,
        )
        listing_row.rarity = listing_rarity
        if offered_rarity != listing_rarity:
            raise S1PlayerVisibleError('只能用同稀有度的物品交换')

        if S1MarketService._warehouse_blocked_by_cap(
            seller_adv.id, name=offered_wh_row.name, item_type=offered_wh_row.type,
            rarity=offered_rarity, custom_desc=offered_wh_row.custom_desc,
            uses_left=offered_wh_row.uses_left,
            recovery_minutes=offered_wh_row.recovery_minutes,
            recovery_started_at=offered_wh_row.recovery_started_at,
            recovery_ends_at=offered_wh_row.recovery_ends_at,
        ):
            raise S1PlayerVisibleError('卖方仓库已满，无法成交')
        if S1MarketService._warehouse_blocked_by_cap(
            buyer_adv.id, name=listing_row.item_name, item_type=listing_row.item_type,
            rarity=listing_row.rarity, custom_desc=listing_row.custom_desc,
            uses_left=listing_row.uses_left,
            recovery_minutes=listing_row.recovery_minutes,
            recovery_started_at=listing_row.recovery_started_at,
            recovery_ends_at=listing_row.recovery_ends_at,
        ):
            raise S1PlayerVisibleError('买方仓库已满，无法成交')

        fee = S1MarketService._fee_for(listing_rarity)
        seller_fee_paid = round(float(listing_row.seller_fee_paid or 0), 2)
        complete_listing = trade_qty >= listing_qty
        offered_name = offered_wh_row.name

        take_from_stack(offered_wh_row, trade_qty)
        S1MarketService._log_market_movement(
            buyer_adv.id, name=offered_wh_row.name, item_type=offered_wh_row.type,
            rarity=offered_rarity, quantity=trade_qty,
            change_type='consume', listing_id=listing_id,
            description='交易市场以物易物（换出）',
        )

        if fee > 0:
            rarity_label = S1MarketService.RARITY_LABELS.get(listing_rarity, listing_rarity)
            currency_entries = []
            if seller_fee_paid <= 0:
                currency_entries.append({
                    'adventurer_id': seller_adv.id,
                    'change_amount': -fee,
                    'source_type': 'market_fee',
                    'related_id': listing_id,
                    'related_type': 'market_listing',
                    'description': f'交易市场手续费（售出·{rarity_label}）',
                    'insufficient_error': '挂单方的金币不足以支付手续费',
                })
            currency_entries.append({
                'adventurer_id': buyer_adv.id,
                'change_amount': -fee,
                'source_type': 'market_fee',
                'related_id': listing_id,
                'related_type': 'market_listing',
                'description': f'交易市场手续费（换入·{rarity_label}）',
                'insufficient_error': f'你的金币不足以支付手续费（{fee} 金币）',
            })
            _, fee_err = S1CurrencyService.apply_changes_in_transaction(
                'gold',
                currency_entries,
            )
            if fee_err:
                raise S1PlayerVisibleError(fee_err)

        qty_note = f' ×{trade_qty}' if trade_qty > 1 else ''
        S1MarketService._deliver_item_to_warehouse(
            buyer_adv.id, name=listing_row.item_name, item_type=listing_row.item_type,
            rarity=listing_rarity, custom_desc=listing_row.custom_desc,
            uses_left=listing_row.uses_left, quantity=trade_qty,
            evolution_stage=listing_row.evolution_stage,
            recovery_minutes=listing_row.recovery_minutes,
            recovery_started_at=listing_row.recovery_started_at,
            recovery_ends_at=listing_row.recovery_ends_at,
            listing_id=listing_id, description=f'交易市场以物易物（换入{qty_note}）',
            escrow_listing_id=listing_id,
        )
        S1MarketService._deliver_item_to_warehouse(
            seller_adv.id, name=offered_wh_row.name, item_type=offered_wh_row.type,
            rarity=offered_rarity, custom_desc=offered_wh_row.custom_desc,
            uses_left=offered_wh_row.uses_left, quantity=trade_qty,
            evolution_stage=offered_wh_row.evolution_stage,
            recovery_minutes=offered_wh_row.recovery_minutes,
            recovery_started_at=offered_wh_row.recovery_started_at,
            recovery_ends_at=offered_wh_row.recovery_ends_at,
            listing_id=listing_id, description=f'交易市场以物易物（换入{qty_note}）',
        )

        now = _portable_utc_now()
        if complete_listing:
            listing_row.status = 'completed'
            listing_row.quantity = 0
            listing_row.buyer_adventurer_id = buyer_adv.id
            listing_row.traded_item_name = offered_name
            listing_row.completed_at = now
        else:
            listing_row.quantity = listing_qty - trade_qty
        listing_row.updated_at = now

        return {
            'trade_quantity': trade_qty,
            'listing_completed': complete_listing,
            'received_item': listing_row.item_name,
            'given_item': offered_name,
            'fee_paid': fee,
            'seller_fee_prepaid': seller_fee_paid > 0,
        }

    @staticmethod
    def _validate_sell_price(rarity: str, price: float, quantity: int = 1) -> Optional[str]:
        """校验卖钱模式的总价是否在允许范围内（区间按数量整体放大）。返回错误信息或 None。

        price 是整堆总价；区间 [单件min×数量, 单件max×数量]，避免「N 件卖单件底价」套利。
        """
        price_range = S1MarketService._get_sell_price_range()
        rng = price_range.get(rarity)
        if not rng:
            return f'稀有度「{rarity}」不支持卖钱'
        qty = max(1, int(quantity or 1))
        p_min = int(rng.get('min', 0)) * qty
        p_max = int(rng.get('max', 999999)) * qty
        qty_note = f'（{qty} 件）' if qty > 1 else ''
        if price < p_min:
            return f'售价不能低于 {p_min} 金币{qty_note}'
        if price > p_max:
            return f'售价不能超过 {p_max} 金币{qty_note}'
        return None

    @staticmethod
    def _check_tradeable_item(item: AdventurerS1WarehouseItem) -> Optional[str]:
        """挂单/出价物品的通用准入校验。返回错误信息或 None。"""
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_PLAYER_DESTRUCTIVE,
            validate_item_operation,
        )

        protection_err = validate_item_operation(
            item,
            ITEM_OPERATION_PLAYER_DESTRUCTIVE,
            action_label='市场交易',
        )
        if protection_err:
            return protection_err
        from plugins.adventurer_s1.seed import is_herbalist_item
        if is_herbalist_item(item.name):
            return f'草药店类物品「{item.name}」不能上市场交易'
        if item.rarity not in S1MarketService.VALID_RARITIES:
            return f'「{item.name}」稀有度异常，无法交易'
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
            return None, reason or '当前状态无法进行交易'
        return adv, None

    @staticmethod
    def _lock_adventurers(adventurer_ids) -> dict[int, AdventurerS1]:
        """按全局稳定顺序锁住市场事务涉及的全部冒险者并刷新 identity map。"""
        normalized_ids = sorted({int(adventurer_id) for adventurer_id in adventurer_ids})
        rows = (
            AdventurerS1.query
            .filter(AdventurerS1.id.in_(normalized_ids))
            .order_by(AdventurerS1.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        row_map = {int(row.id): row for row in rows}
        if len(row_map) != len(normalized_ids):
            raise RuntimeError('交易冒险者不存在')
        return row_map

    @staticmethod
    def _lock_warehouse_aggregates(adventurer_ids) -> None:
        """Lock warehouse aggregates in the same order before any child inventory row."""
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            lock_warehouse_aggregates,
        )

        lock_warehouse_aggregates(adventurer_ids)

    @staticmethod
    def _item_desc_map(names: set) -> dict:
        if not names:
            return {}
        return {
            it.name: it.description
            for it in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
        }

    _SEARCH_MAX_LEN = 50

    @staticmethod
    def _escape_ilike_pattern(raw: str) -> str:
        return raw.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    @staticmethod
    def _normalize_search(search: Optional[str]) -> Optional[str]:
        if search is None:
            return None
        keyword = search.strip()
        if not keyword:
            return None
        return keyword[:S1MarketService._SEARCH_MAX_LEN]

    @staticmethod
    def _apply_item_search(query, search: Optional[str]):
        """按物品名、有效描述、想要备注或卖方冒险者名模糊匹配。"""
        keyword = S1MarketService._normalize_search(search)
        if not keyword:
            return query
        escaped = S1MarketService._escape_ilike_pattern(keyword)
        pattern = f'%{escaped}%'
        default_desc_matches = exists().where(and_(
            AdventurerS1Item.name == AdventurerS1MarketListing.item_name,
            AdventurerS1Item.description.ilike(pattern, escape='\\'),
        ))
        seller_name_matches = exists().where(and_(
            AdventurerS1.id == AdventurerS1MarketListing.seller_adventurer_id,
            AdventurerS1.name.ilike(pattern, escape='\\'),
        ))
        return query.filter(or_(
            AdventurerS1MarketListing.item_name.ilike(pattern, escape='\\'),
            AdventurerS1MarketListing.want_note.ilike(pattern, escape='\\'),
            AdventurerS1MarketListing.custom_desc.ilike(pattern, escape='\\'),
            and_(
                or_(
                    AdventurerS1MarketListing.custom_desc.is_(None),
                    AdventurerS1MarketListing.custom_desc == '',
                ),
                default_desc_matches,
            ),
            seller_name_matches,
        ))

    @staticmethod
    def _adv_name_map(adv_ids: set) -> dict:
        if not adv_ids:
            return {}
        return dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adv_ids)).all()
        )

    @staticmethod
    def _require_market_built() -> Optional[str]:
        """个人交易市场须已建成才允许交易操作。"""
        from plugins.adventurer_s1.models import AdventurerS1BuildingInstance, AdventurerS1BuildingTemplate
        built = (
            AdventurerS1BuildingInstance.query
            .join(AdventurerS1BuildingTemplate)
            .filter(
                AdventurerS1BuildingTemplate.category == 'market',
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.status == 'built',
                AdventurerS1BuildingInstance.disabled.is_(False),
            ).first()
        )
        if not built:
            return '个人交易市场尚未建成，暂无法交易'
        return None

    @staticmethod
    def _emit_market_changed(action: str, listing_id: int) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_s1_market_changed', {
                'action': action, 'listing_id': listing_id,
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def _emit_adventurers_changed(action: str, adventurer_ids: set[int]) -> None:
        if not adventurer_ids:
            return
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': action,
                'ids': sorted(int(adventurer_id) for adventurer_id in adventurer_ids),
            }, namespace='/')
        except Exception:
            pass

    # ── 格子管理 ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_slot_info(adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """获取冒险者的挂单格子信息（当前占用 / 基础上限 / 额外格子 / 到期时间）。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'

        base_slots = int(current_app.config.get('ADVENTURER_S1_MARKET_MAX_LISTINGS_PER_ADV', 3))
        extra_slots = int(adv.market_extra_slots or 0)
        expires_at = adv.market_extra_slots_expires_at

        # 过期则清零
        now = _portable_utc_now()
        if expires_at and expires_at <= now:
            extra_slots = 0
            expires_at = None

        active_count = AdventurerS1MarketListing.query.filter_by(
            seller_adventurer_id=adventurer_id, status='active',
        ).count()

        return {
            'active_count': active_count,
            'base_slots': base_slots,
            'extra_slots': extra_slots,
            'max_slots': base_slots + extra_slots,
            'extra_slots_expires_at': expires_at.isoformat() + 'Z' if expires_at else None,
        }, None

    @staticmethod
    def purchase_listing_slots(user_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """花 50 金币购买 2 个额外挂单格子，24 小时有效。可叠加续期。"""
        slot_cost = float(current_app.config.get('ADVENTURER_S1_MARKET_SLOT_COST', 50.0))
        slot_count = int(current_app.config.get('ADVENTURER_S1_MARKET_SLOT_COUNT', 2))
        slot_duration_hours = int(current_app.config.get('ADVENTURER_S1_MARKET_SLOT_DURATION_HOURS', 24))

        adv, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        # 扣金币
        balance, bal_err = S1CurrencyService.get_balance(adventurer_id, 'gold')
        if bal_err:
            return None, bal_err
        if balance is None or balance < slot_cost - 0.001:
            return None, f'金币不足（需要 {slot_cost} 金币）'

        now = _portable_utc_now()
        new_expires = now

        try:
            with db_manager.transaction():
                locked_adv = S1MarketService._lock_adventurers([adventurer_id])[adventurer_id]
                if locked_adv.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                can_trade, reason = locked_adv.can_dispatch()
                if not can_trade:
                    raise S1PlayerVisibleError(reason or '当前状态无法进行交易')

                current_expires = locked_adv.market_extra_slots_expires_at
                base_time = current_expires if (current_expires and current_expires > now) else now
                new_expires = base_time + timedelta(hours=slot_duration_hours)
                _, deduct_err = S1CurrencyService.apply_changes_in_transaction(
                    'gold',
                    [{
                        'adventurer_id': adventurer_id,
                        'change_amount': -slot_cost,
                        'source_type': 'market_slot_purchase',
                        'description': (
                            f'购买交易市场扩展格子'
                            f'（+{slot_count} 格·{slot_duration_hours}h）'
                        ),
                        'insufficient_error': f'金币不足（需要 {slot_cost} 金币）',
                    }],
                )
                if deduct_err:
                    raise S1PlayerVisibleError(deduct_err)

                locked_adv.market_extra_slots = slot_count
                locked_adv.market_extra_slots_expires_at = new_expires
                locked_adv.updated_at = now
        except Exception as e:
            return None, player_operation_failure(e, '购买失败')

        S1MarketService._emit_market_changed('slots_purchased', 0)
        return {
            'extra_slots': slot_count,
            'expires_at': new_expires.isoformat() + 'Z',
            'cost': slot_cost,
        }, None

    @staticmethod
    def _get_effective_max_listings(adventurer_id: int) -> int:
        """获取冒险者当前有效的最大挂单数（基础 + 未过期的额外格子）。"""
        base_slots = int(current_app.config.get('ADVENTURER_S1_MARKET_MAX_LISTINGS_PER_ADV', 3))
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return base_slots

        extra = int(adv.market_extra_slots or 0)
        expires_at = adv.market_extra_slots_expires_at
        if extra > 0 and expires_at and expires_at <= _portable_utc_now():
            extra = 0
        return base_slots + extra

    # ── 查询 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _pending_offer_counts_for_user(user_id: int) -> dict[int, int]:
        """按挂单聚合当前玩家收到的待处理报价，仅供卖家提示。"""
        rows = (
            db_manager.query(
                AdventurerS1MarketOffer.listing_id,
                func.count(AdventurerS1MarketOffer.id),
            )
            .join(
                AdventurerS1MarketListing,
                AdventurerS1MarketListing.id == AdventurerS1MarketOffer.listing_id,
            )
            .join(
                AdventurerS1,
                AdventurerS1.id == AdventurerS1MarketListing.seller_adventurer_id,
            )
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1MarketListing.status == 'active',
                AdventurerS1MarketListing.listing_type == 'barter',
                AdventurerS1MarketOffer.status == 'pending',
            )
            .group_by(AdventurerS1MarketOffer.listing_id)
            .all()
        )
        return {int(listing_id): int(count) for listing_id, count in rows}

    @staticmethod
    def list_listings(
        page: int, per_page: int,
        rarity: str = None, listing_type: str = None, search: str = None,
        user_id: int | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """市场在售挂单（分页，可按稀有度/类型/关键词过滤），附手续费表和卖钱价格范围。"""
        query = AdventurerS1MarketListing.query.filter(
            AdventurerS1MarketListing.status == 'active',
        )
        if rarity:
            if rarity not in S1MarketService.VALID_RARITIES:
                return None, '无效的稀有度'
            query = query.filter_by(rarity=rarity)
        if listing_type in ('barter', 'sell'):
            query = query.filter_by(listing_type=listing_type)
        query = S1MarketService._apply_item_search(query, search)
        total = query.count()
        rows = (
            query.order_by(AdventurerS1MarketListing.created_at.desc())
            .offset((page - 1) * per_page).limit(per_page).all()
        )

        desc_map = S1MarketService._item_desc_map({r.item_name for r in rows})
        name_map = S1MarketService._adv_name_map({r.seller_adventurer_id for r in rows})
        items = [
            r.to_dict(item_dict=desc_map, seller_name=name_map.get(r.seller_adventurer_id, '?'))
            for r in rows
        ]
        pending_offer_counts = (
            S1MarketService._pending_offer_counts_for_user(user_id) if user_id is not None else {}
        )
        for item in items:
            if item['id'] in pending_offer_counts:
                item['offers_count'] = pending_offer_counts[item['id']]
        S1MarketService._finalize_market_client_payload(items)
        data = build_paginated_response(items, total, page, per_page)
        data['my_pending_offer_count'] = sum(pending_offer_counts.values())
        data['fees'] = S1MarketService._get_fees()
        data['sell_price_range'] = S1MarketService._get_sell_price_range()
        data['sell_fee_rate'] = S1MarketService._get_sell_fee_rate()
        return data, None

    @staticmethod
    def get_my_listings(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """我的挂单：名下冒险者的在售全部 + 最近 10 条已成交/已取消回执。"""
        adv_ids = [
            row[0] for row in
            AdventurerS1.query.with_entities(AdventurerS1.id)
            .filter_by(owner_id=user_id, is_player_created=True).all()
        ]
        if not adv_ids:
            return {'active': [], 'history': []}, None

        active_rows = (
            AdventurerS1MarketListing.query
            .filter(AdventurerS1MarketListing.seller_adventurer_id.in_(adv_ids),
                    AdventurerS1MarketListing.status == 'active')
            .order_by(AdventurerS1MarketListing.created_at.desc()).all()
        )
        history_rows = (
            AdventurerS1MarketListing.query
            .filter(AdventurerS1MarketListing.seller_adventurer_id.in_(adv_ids),
                    AdventurerS1MarketListing.status != 'active')
            .order_by(AdventurerS1MarketListing.updated_at.desc()).limit(10).all()
        )

        all_rows = active_rows + history_rows
        desc_map = S1MarketService._item_desc_map({r.item_name for r in all_rows})
        buyer_map = S1MarketService._adv_name_map(
            {r.buyer_adventurer_id for r in history_rows if r.buyer_adventurer_id}
        )
        active_items = [r.to_dict(item_dict=desc_map, include_trade_info=True) for r in active_rows]
        history_items = [
            r.to_dict(item_dict=desc_map, include_trade_info=True,
                      buyer_name=buyer_map.get(r.buyer_adventurer_id))
            for r in history_rows
        ]
        S1MarketService._finalize_market_client_payload(active_items, history_items)
        return {
            'active': active_items,
            'history': history_items,
        }, None

    # ── 挂单 / 取消 ─────────────────────────────────────────────────────────

    @staticmethod
    def _log_market_movement(
        adventurer_id: int, *, name: str, item_type, rarity,
        quantity, change_type: str, listing_id: int, description: str,
        related_type: str = 'market_listing',
    ) -> None:
        """记一条 market 物品流水（consume=离开本人 / gain=进入本人）。

        供数据申诉「卖了/换了说不见」查证：离开本人仓库一律记 consume(source=market)，
        一审 recent_activity 即可查到。须在调用方的 db_manager.transaction() 内调用（仅 flush）。
        """
        db_manager.add(AdventurerS1ItemLog(
            adventurer_id=adventurer_id,
            item_name=name,
            item_type=item_type or '',
            item_rarity=rarity or 'common',
            change_type=change_type,
            quantity=int(quantity or 1),
            source_type='market',
            related_id=str(listing_id),
            related_type=related_type,
            description=description,
        ), flush=False)

    @staticmethod
    def _deliver_item_to_warehouse(
        adventurer_id: int, *, name: str, item_type, rarity, custom_desc, uses_left,
        quantity, listing_id: int, description: str, evolution_stage: int = 0,
        recovery_minutes: int = 0, recovery_started_at=None, recovery_ends_at=None,
        related_type: str = 'market_listing',
        escrow_listing_id: int | None = None,
        adventurer=None,
        warehouse_batch_context=None,
    ) -> None:
        """把成交/退回的物品并入仓库（走统一 grant 链路：堆叠 + ItemLog + 动态）。"""
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item

        adv = adventurer or AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            raise S1PlayerVisibleError('冒险者不存在')
        if int(adv.id) != int(adventurer_id):
            raise RuntimeError('入库目标冒险者不一致')
        _, err = grant_raw_item(
            adv,
            name,
            {
                'type': item_type or '',
                'rarity': rarity or 'common',
                'quantity': quantity,
                'uses_left': uses_left,
                'desc': custom_desc or '',
                'evolution_stage': evolution_stage,
                'recovery_minutes': recovery_minutes,
                'recovery_started_at': recovery_started_at,
                'recovery_ends_at': recovery_ends_at,
            },
            source_type='market',
            description=description,
            force_destination='warehouse',
            related_id=str(listing_id),
            related_type=related_type,
            world_unique_escrow_listing_id=escrow_listing_id,
            world_unique_escrow_quantity=(
                quantity if escrow_listing_id is not None else None
            ),
            warehouse_batch_context=warehouse_batch_context,
        )
        if err:
            # grant_raw_item 的错误返回仅为校验原因；技术故障直接抛出异常。
            raise S1PlayerVisibleError(err)

    @staticmethod
    def _warehouse_blocked_by_cap(
        adventurer_id: int, *, name, item_type, rarity, custom_desc, uses_left,
        recovery_minutes: int = 0, recovery_started_at=None, recovery_ends_at=None,
    ) -> bool:
        """仓库满时是否真的无法接收：仅完整实例属性一致时允许合堆。"""
        from plugins.adventurer_s1.services.s1_item_grant_service import warehouse_grant_blocked_by_capacity
        return warehouse_grant_blocked_by_capacity(
            adventurer_id,
            name=name, item_type=item_type or '', rarity=rarity or 'common',
            custom_desc=custom_desc, uses_left=uses_left,
            recovery_minutes=recovery_minutes,
            recovery_started_at=recovery_started_at,
            recovery_ends_at=recovery_ends_at,
        )

    @staticmethod
    def create_listing(
        user_id: int, adventurer_id: int,
        warehouse_item_id: Optional[int] = None, item_name: Optional[str] = None,
        listing_type: str = 'barter', price: Optional[float] = None,
        want_note: Optional[str] = None, quantity: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """把仓库物品挂上市场。优先 warehouse_item_id；仅一件同名时可用 item_name。

        listing_type='barter'：同稀有度以物易物（现有逻辑）
        listing_type='sell'：卖钱模式，需传 price
        quantity 为 None → 整堆挂出（向后兼容）；否则按数量拆堆，剩余留在仓库。
        """
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            resolve_move_quantity, resolve_warehouse_item, take_from_stack,
        )

        if listing_type not in ('barter', 'sell'):
            return None, '无效的挂单类型'

        # want_note 仅 barter 模式有效，截断到 200 字符
        if want_note is not None:
            want_note = str(want_note).strip()[:200] or None
        if listing_type != 'barter':
            want_note = None

        err = S1MarketService._require_market_built()
        if err:
            return None, err
        adv, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        max_listings = S1MarketService._get_effective_max_listings(adventurer_id)
        active_count = AdventurerS1MarketListing.query.filter_by(
            seller_adventurer_id=adventurer_id, status='active',
        ).count()
        if active_count >= max_listings:
            return None, f'同时挂单已达上限 {max_listings} 件'

        wh_item, err = resolve_warehouse_item(
            adventurer_id, item_id=warehouse_item_id, item_name=item_name,
        )
        if err:
            return None, err
        err = S1MarketService._check_tradeable_item(wh_item)
        if err:
            return None, err

        list_qty, qty_err = resolve_move_quantity(wh_item, quantity)
        if qty_err:
            return None, qty_err
        clue_rarity_map = S1MarketService._clue_catalog_rarity_map({wh_item.name})
        effective_rarity = S1MarketService._effective_item_rarity(
            wh_item.name,
            wh_item.rarity,
            clue_rarity_map,
        )

        # 卖钱模式：校验价格
        if listing_type == 'sell':
            if price is None:
                return None, '卖钱模式必须设定售价'
            try:
                price = round(float(price), 2)
            except (TypeError, ValueError):
                return None, '售价格式不正确'
            if not math.isfinite(price) or price <= 0:
                return None, '售价必须大于 0'
            price_err = S1MarketService._validate_sell_price(effective_rarity, price, list_qty)
            if price_err:
                return None, price_err

        # barter 模式：挂单时预扣卖方手续费
        fee = 0.0
        if listing_type == 'barter':
            fee = S1MarketService._fee_for(effective_rarity)
            if fee > 0:
                balance, bal_err = S1CurrencyService.get_balance(adventurer_id, 'gold')
                if bal_err:
                    return None, bal_err
                if balance is None or balance < fee - 0.001:
                    return None, f'金币不足以预付手续费（{fee} 金币）'

        listing = AdventurerS1MarketListing(
            seller_adventurer_id=adventurer_id,
            item_name=wh_item.name,
            item_type=wh_item.type,
            rarity=effective_rarity,
            custom_desc=wh_item.custom_desc,
            quantity=list_qty,
            uses_left=wh_item.uses_left,
            evolution_stage=wh_item.evolution_stage,
            recovery_minutes=wh_item.recovery_minutes,
            recovery_started_at=wh_item.recovery_started_at,
            recovery_ends_at=wh_item.recovery_ends_at,
            listing_type=listing_type,
            price=price if listing_type == 'sell' else None,
            want_note=want_note if listing_type == 'barter' else None,
            status='active',
            seller_fee_paid=fee,
        )
        rarity_label = S1MarketService.RARITY_LABELS.get(effective_rarity, effective_rarity)
        try:
            with db_manager.transaction():
                locked_adv = S1MarketService._lock_adventurers([adventurer_id])[adventurer_id]
                S1MarketService._lock_warehouse_aggregates([adventurer_id])
                if locked_adv.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                can_trade, reason = locked_adv.can_dispatch()
                if not can_trade:
                    raise S1PlayerVisibleError(reason or '当前状态无法进行交易')
                locked_max_listings = S1MarketService._get_effective_max_listings(adventurer_id)
                locked_active_count = AdventurerS1MarketListing.query.filter_by(
                    seller_adventurer_id=adventurer_id,
                    status='active',
                ).count()
                if locked_active_count >= locked_max_listings:
                    raise S1PlayerVisibleError(f'同时挂单已达上限 {locked_max_listings} 件')

                wh_row = (
                    AdventurerS1WarehouseItem.query
                    .filter_by(id=wh_item.id, adventurer_id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not wh_row:
                    raise S1PlayerVisibleError(f'「{wh_item.name}」已不在仓库')
                tradeable_err = S1MarketService._check_tradeable_item(wh_row)
                if tradeable_err:
                    raise S1PlayerVisibleError(tradeable_err)
                move_qty, qty_err = resolve_move_quantity(wh_row, quantity)
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                effective_rarity = S1MarketService._effective_item_rarity(
                    wh_row.name,
                    wh_row.rarity,
                )

                if listing_type == 'sell':
                    price_err = S1MarketService._validate_sell_price(
                        effective_rarity,
                        price,
                        move_qty,
                    )
                    if price_err:
                        raise S1PlayerVisibleError(price_err)
                    fee = 0.0
                else:
                    fee = S1MarketService._fee_for(effective_rarity)

                listing.item_name = wh_row.name
                listing.item_type = wh_row.type
                listing.rarity = effective_rarity
                listing.custom_desc = wh_row.custom_desc
                listing.quantity = move_qty
                listing.uses_left = wh_row.uses_left
                listing.evolution_stage = wh_row.evolution_stage
                listing.recovery_minutes = wh_row.recovery_minutes
                listing.recovery_started_at = wh_row.recovery_started_at
                listing.recovery_ends_at = wh_row.recovery_ends_at
                listing.seller_fee_paid = fee
                rarity_label = S1MarketService.RARITY_LABELS.get(
                    effective_rarity,
                    effective_rarity,
                )
                take_from_stack(wh_row, move_qty)
                ok, add_err = db_manager.add(listing)
                if not ok:
                    raise RuntimeError(add_err)
                S1MarketService._log_market_movement(
                    adventurer_id, name=listing.item_name, item_type=listing.item_type,
                    rarity=listing.rarity, quantity=item_quantity_of(listing),
                    change_type='consume', listing_id=listing.id,
                    description=f'挂上交易市场（{rarity_label}）',
                )
                if fee > 0:
                    _, fee_err = S1CurrencyService.apply_changes_in_transaction(
                        'gold',
                        [{
                            'adventurer_id': adventurer_id,
                            'change_amount': -fee,
                            'source_type': 'market_fee',
                            'related_id': listing.id,
                            'related_type': 'market_listing',
                            'description': f'交易市场手续费（挂单预扣·{rarity_label}）',
                            'insufficient_error': f'金币不足以预付手续费（{fee} 金币）',
                        }],
                    )
                    if fee_err:
                        raise S1PlayerVisibleError(fee_err)
        except Exception as e:
            return None, player_operation_failure(e, '挂单失败')

        S1MarketService._emit_market_changed('listed', listing.id)
        desc_map = S1MarketService._item_desc_map({listing.item_name})
        listing_dict = listing.to_dict(item_dict=desc_map, include_trade_info=True)
        S1MarketService._finalize_market_client_payload(listing_dict)
        return listing_dict, None

    @staticmethod
    def cancel_listing(user_id: int, listing_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """取消挂单，物品退回仓库；barter 模式退还挂单时预扣的卖方手续费。"""
        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing:
            return None, '挂单不存在'
        adv, err = S1MarketService._get_owned_adventurer(listing.seller_adventurer_id, user_id)
        if err:
            return None, err
        if listing.status != 'active':
            return None, '该挂单已成交或已取消'

        refund_fee = round(float(listing.seller_fee_paid or 0), 2)
        rarity_label = S1MarketService.RARITY_LABELS.get(listing.rarity, listing.rarity)
        cancel_item_name = listing.item_name
        try:
            with db_manager.transaction():
                locked_adv = S1MarketService._lock_adventurers([adv.id])[adv.id]
                S1MarketService._lock_warehouse_aggregates([adv.id])
                if locked_adv.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                can_trade, reason = locked_adv.can_dispatch()
                if not can_trade:
                    raise S1PlayerVisibleError(reason or '当前状态无法进行交易')
                listing_row = (
                    AdventurerS1MarketListing.query
                    .filter_by(id=listing_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not listing_row or listing_row.seller_adventurer_id != locked_adv.id:
                    raise S1PlayerVisibleError('该挂单刚刚已被换走')
                effective_rarity = S1MarketService._effective_item_rarity(
                    listing_row.item_name,
                    listing_row.rarity,
                )
                listing_row.rarity = effective_rarity
                if S1MarketService._warehouse_blocked_by_cap(
                    locked_adv.id,
                    name=listing_row.item_name,
                    item_type=listing_row.item_type,
                    rarity=effective_rarity,
                    custom_desc=listing_row.custom_desc,
                    uses_left=listing_row.uses_left,
                    recovery_minutes=listing_row.recovery_minutes,
                    recovery_started_at=listing_row.recovery_started_at,
                    recovery_ends_at=listing_row.recovery_ends_at,
                ):
                    wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
                    raise S1PlayerVisibleError(f'仓库已满（{wh_cap} 件），无法退回物品')

                refund_fee = round(float(listing_row.seller_fee_paid or 0), 2)
                rarity_label = S1MarketService.RARITY_LABELS.get(
                    effective_rarity,
                    effective_rarity,
                )
                cancel_item_name = listing_row.item_name
                S1MarketService._deliver_item_to_warehouse(
                    locked_adv.id, name=listing_row.item_name, item_type=listing_row.item_type,
                    rarity=effective_rarity, custom_desc=listing_row.custom_desc,
                    uses_left=listing_row.uses_left, quantity=item_quantity_of(listing_row),
                    evolution_stage=listing_row.evolution_stage,
                    recovery_minutes=listing_row.recovery_minutes,
                    recovery_started_at=listing_row.recovery_started_at,
                    recovery_ends_at=listing_row.recovery_ends_at,
                    listing_id=listing_id,
                    description=f'取消挂单退回仓库（{rarity_label}）',
                    escrow_listing_id=listing_id,
                )
                listing_row.status = 'cancelled'
                listing_row.updated_at = _portable_utc_now()
                if refund_fee > 0:
                    _, refund_err = S1CurrencyService.apply_changes_in_transaction(
                        'gold',
                        [{
                            'adventurer_id': locked_adv.id,
                            'change_amount': refund_fee,
                            'source_type': 'market_fee_refund',
                            'related_id': listing_id,
                            'related_type': 'market_listing',
                            'description': f'交易市场手续费退还（下架·{rarity_label}）',
                        }],
                    )
                    if refund_err:
                        raise RuntimeError(refund_err)
        except Exception as e:
            return None, player_operation_failure(e, '取消失败')

        S1MarketService._emit_market_changed('cancelled', listing_id)
        cancel_payload = {
            'listing_id': listing_id,
            'item_name': cancel_item_name,
            'fee_refunded': refund_fee,
        }
        S1MarketService._finalize_market_client_payload(cancel_payload)
        return cancel_payload, None

    # ── 卖钱购买 ────────────────────────────────────────────────────────────

    @staticmethod
    def buy_listing(
        user_id: int, adventurer_id: int, listing_id: int,
        quantity: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """用金币购买 sell 模式的挂单物品。可指定 quantity 部分购买，按单价比例扣费。"""
        err = S1MarketService._require_market_built()
        if err:
            return None, err
        buyer_adv, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing or listing.status != 'active':
            return None, '该挂单已成交或已下架'
        if listing.listing_type != 'sell':
            return None, '该挂单不是卖钱模式，请用交换功能'
        if listing.seller_adventurer_id == buyer_adv.id:
            return None, '不能购买自己挂的物品'

        seller_adv = AdventurerS1.query.session.get(AdventurerS1, listing.seller_adventurer_id)
        if not seller_adv:
            return None, '挂单方冒险者不存在'

        listing_qty = item_quantity_of(listing)
        listing_rarity = S1MarketService._effective_item_rarity(
            listing.item_name,
            listing.rarity,
        )
        listing_total_price = S1MarketService._clamp_sell_total_price(
            listing_rarity,
            listing.price or 0,
            listing_qty,
        )
        buy_qty, price, qty_err = S1MarketService._calc_sell_buy_totals(
            listing_total_price, listing_qty, quantity,
        )
        if qty_err:
            return None, qty_err
        if price <= 0:
            return None, '挂单价格异常'

        sell_fee_rate = S1MarketService._get_sell_fee_rate()
        seller_income = round(price * (1 - sell_fee_rate), 2)
        market_fee = round(price - seller_income, 2)
        complete_listing = buy_qty >= listing_qty

        buyer_balance, bal_err = S1CurrencyService.get_balance(buyer_adv.id, 'gold')
        if bal_err:
            return None, bal_err
        if buyer_balance is None or buyer_balance < price - 0.001:
            return None, f'金币不足（需要 {price} 金币）'

        rarity_label = S1MarketService.RARITY_LABELS.get(listing_rarity, listing_rarity)
        qty_note = f' ×{buy_qty}' if buy_qty > 1 else ''
        remaining_qty = None
        remaining_price = None
        result_item_name = listing.item_name
        try:
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers([
                    buyer_adv.id,
                    seller_adv.id,
                ])
                S1MarketService._lock_warehouse_aggregates(locked_map)
                buyer_row = locked_map[buyer_adv.id]
                seller_row = locked_map[seller_adv.id]
                if buyer_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                buyer_can, buyer_reason = buyer_row.can_dispatch()
                if not buyer_can:
                    raise S1PlayerVisibleError(buyer_reason or '当前状态无法进行交易')
                listing_row = (
                    AdventurerS1MarketListing.query
                    .filter_by(id=listing_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not listing_row:
                    raise S1PlayerVisibleError('该挂单刚刚已被他人买走')
                if listing_row.listing_type != 'sell':
                    raise S1PlayerVisibleError('该挂单不是卖钱模式，请用交换功能')
                if listing_row.seller_adventurer_id != seller_row.id:
                    raise S1PlayerVisibleError('挂单方已变化，请刷新后重试')

                current_qty = item_quantity_of(listing_row)
                effective_rarity = S1MarketService._effective_item_rarity(
                    listing_row.item_name,
                    listing_row.rarity,
                )
                listing_row.rarity = effective_rarity
                effective_total_price = S1MarketService._clamp_sell_total_price(
                    effective_rarity,
                    listing_row.price or 0,
                    current_qty,
                )
                listing_row.price = effective_total_price
                buy_qty, price, qty_err = S1MarketService._calc_sell_buy_totals(
                    effective_total_price, current_qty, quantity,
                )
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                if price <= 0:
                    raise S1PlayerVisibleError('挂单价格异常')

                seller_income = round(price * (1 - sell_fee_rate), 2)
                market_fee = round(price - seller_income, 2)
                complete_listing = buy_qty >= current_qty
                qty_note = f' ×{buy_qty}' if buy_qty > 1 else ''
                rarity_label = S1MarketService.RARITY_LABELS.get(
                    effective_rarity,
                    effective_rarity,
                )
                result_item_name = listing_row.item_name

                currency_entries = [{
                    'adventurer_id': buyer_row.id,
                    'change_amount': -price,
                    'source_type': 'market_buy',
                    'related_id': listing_id,
                    'related_type': 'market_listing',
                    'description': (
                        f'交易市场购买「{listing_row.item_name}」{qty_note}'
                        f'（{rarity_label}）'
                    ),
                    'insufficient_error': f'金币不足（需要 {price} 金币）',
                }]
                if seller_income > 0:
                    currency_entries.append({
                        'adventurer_id': seller_row.id,
                        'change_amount': seller_income,
                        'source_type': 'market_sell',
                        'related_id': listing_id,
                        'related_type': 'market_listing',
                        'description': (
                            f'交易市场售出「{listing_row.item_name}」{qty_note}'
                            f'（{rarity_label}，手续费 {market_fee}）'
                        ),
                    })
                _, currency_err = S1CurrencyService.apply_changes_in_transaction(
                    'gold',
                    currency_entries,
                )
                if currency_err:
                    raise S1PlayerVisibleError(currency_err)

                S1MarketService._deliver_item_to_warehouse(
                    buyer_row.id, name=listing_row.item_name, item_type=listing_row.item_type,
                    rarity=effective_rarity, custom_desc=listing_row.custom_desc,
                    uses_left=listing_row.uses_left, quantity=buy_qty,
                    evolution_stage=listing_row.evolution_stage,
                    recovery_minutes=listing_row.recovery_minutes,
                    recovery_started_at=listing_row.recovery_started_at,
                    recovery_ends_at=listing_row.recovery_ends_at,
                    listing_id=listing_id,
                    description=f'交易市场购入（{rarity_label}）',
                    escrow_listing_id=listing_id,
                )

                now = _portable_utc_now()
                if complete_listing:
                    listing_row.status = 'completed'
                    listing_row.buyer_adventurer_id = buyer_adv.id
                    listing_row.seller_fee_paid = market_fee
                    listing_row.completed_at = now
                    listing_row.updated_at = now
                else:
                    remaining_qty = current_qty - buy_qty
                    remaining_price = round(effective_total_price - price, 2)
                    listing_row.quantity = remaining_qty
                    listing_row.price = remaining_price
                    listing_row.updated_at = now
        except Exception as e:
            return None, player_operation_failure(e, '购买失败')

        action = 'completed' if complete_listing else 'partial_buy'
        S1MarketService._emit_market_changed(action, listing_id)
        result = {
            'listing_id': listing_id,
            'item_name': result_item_name,
            'quantity_bought': buy_qty,
            'price_paid': price,
            'seller_income': seller_income,
            'market_fee': market_fee,
            'listing_completed': complete_listing,
        }
        if not complete_listing:
            result['listing_remaining_quantity'] = remaining_qty
            result['listing_remaining_price'] = remaining_price
        S1MarketService._finalize_market_client_payload(result)
        return result, None

    # ── 交换（核心，barter 模式） ───────────────────────────────────────────

    @staticmethod
    def exchange(
        user_id: int, adventurer_id: int, listing_id: int,
        offered_warehouse_item_id: Optional[int] = None,
        offered_item_name: Optional[str] = None,
        quantity: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """用自己仓库的同稀有度物品换走挂单物品。quantity 省略=换尽剩余；否则按件互换。"""
        from plugins.adventurer_s1.services.s1_item_instance_service import resolve_warehouse_item

        err = S1MarketService._require_market_built()
        if err:
            return None, err
        buyer_adv, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing or listing.status != 'active':
            return None, '该挂单已成交或已下架'
        if listing.listing_type == 'sell':
            return None, '该挂单是卖钱模式，请用购买功能'
        if listing.seller_adventurer_id == buyer_adv.id:
            return None, '不能交换自己挂的物品'

        seller_adv = AdventurerS1.query.session.get(AdventurerS1, listing.seller_adventurer_id)
        if not seller_adv:
            return None, '挂单方冒险者不存在'

        offered, err = resolve_warehouse_item(
            buyer_adv.id,
            item_id=offered_warehouse_item_id,
            item_name=offered_item_name,
        )
        if err:
            return None, err
        trade_qty, qty_err = S1MarketService._resolve_barter_trade_qty(
            item_quantity_of(listing), quantity, item_quantity_of(offered),
        )
        if qty_err:
            return None, qty_err

        listing_rarity = S1MarketService._effective_item_rarity(
            listing.item_name,
            listing.rarity,
        )
        fee = S1MarketService._fee_for(listing_rarity)
        seller_fee_paid = round(float(listing.seller_fee_paid or 0), 2)
        if seller_fee_paid <= 0 and fee > 0:
            seller_balance, bal_err = S1CurrencyService.get_balance(seller_adv.id, 'gold')
            if bal_err:
                return None, bal_err
            if seller_balance is None or seller_balance < fee - 0.001:
                return None, '挂单方的金币不足以支付手续费，暂无法成交'
        if fee > 0:
            buyer_balance, bal_err = S1CurrencyService.get_balance(buyer_adv.id, 'gold')
            if bal_err:
                return None, bal_err
            if buyer_balance is None or buyer_balance < fee - 0.001:
                return None, f'你的金币不足以支付手续费（{fee} 金币）'

        try:
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers([
                    buyer_adv.id,
                    seller_adv.id,
                ])
                S1MarketService._lock_warehouse_aggregates(locked_map)
                buyer_row = locked_map[buyer_adv.id]
                seller_row = locked_map[seller_adv.id]
                if buyer_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                buyer_can, buyer_reason = buyer_row.can_dispatch()
                if not buyer_can:
                    raise S1PlayerVisibleError(buyer_reason or '当前状态无法进行交易')
                offered_row = (
                    AdventurerS1WarehouseItem.query
                    .filter_by(id=offered.id, adventurer_id=buyer_adv.id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not offered_row:
                    raise S1PlayerVisibleError(f'「{offered.name}」已不在你的仓库')
                listing_row = (
                    AdventurerS1MarketListing.query
                    .filter_by(id=listing_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not listing_row:
                    raise S1PlayerVisibleError('该挂单刚刚已被他人换走')
                if listing_row.listing_type != 'barter':
                    raise S1PlayerVisibleError('该挂单是卖钱模式，请用购买功能')
                if listing_row.seller_adventurer_id != seller_row.id:
                    raise S1PlayerVisibleError('挂单方已变化，请刷新后重试')
                trade_qty, qty_err = S1MarketService._resolve_barter_trade_qty(
                    item_quantity_of(listing_row), quantity, item_quantity_of(offered_row),
                )
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                result = S1MarketService._execute_barter_trade(
                    listing_row, seller_row, buyer_row, offered_row, trade_qty, listing_id,
                )
        except Exception as e:
            return None, player_operation_failure(e, '交换失败')

        action = 'completed' if result.get('listing_completed') else 'partial_exchange'
        S1MarketService._emit_market_changed(action, listing_id)
        exchange_payload = {'listing_id': listing_id, **result}
        S1MarketService._finalize_market_client_payload(exchange_payload)
        return exchange_payload, None

    # ── 备注修改 ─────────────────────────────────────────────────────────────

    @staticmethod
    def update_want_note(user_id: int, listing_id: int, want_note: str) -> Tuple[Optional[dict], Optional[str]]:
        """卖家修改 barter 挂单的「想要什么」备注。"""
        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing:
            return None, '挂单不存在'
        adv, err = S1MarketService._get_owned_adventurer(listing.seller_adventurer_id, user_id)
        if err:
            return None, err
        if listing.status != 'active':
            return None, '该挂单已成交或已取消'
        if listing.listing_type != 'barter':
            return None, '只有以物易物模式可以设置备注'

        note = str(want_note).strip()[:200] or None
        AdventurerS1MarketListing.query.filter_by(id=listing_id).update({
            AdventurerS1MarketListing.want_note: note,
            AdventurerS1MarketListing.updated_at: _portable_utc_now(),
        }, synchronize_session=False)
        db_manager.commit()
        S1MarketService._emit_market_changed('note_updated', listing_id)
        return {'listing_id': listing_id, 'want_note': note}, None

    # ── 买家出价 ─────────────────────────────────────────────────────────────

    @staticmethod
    def submit_offer(
        user_id: int, adventurer_id: int, listing_id: int,
        offered_warehouse_item_id: Optional[int] = None,
        offered_item_name: Optional[str] = None,
        quantity: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """买家对 barter 挂单提交出价。quantity 为期望互换件数（换出=换入）。"""
        from plugins.adventurer_s1.services.s1_item_instance_service import resolve_warehouse_item

        err = S1MarketService._require_market_built()
        if err:
            return None, err
        buyer_adv, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err

        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing or listing.status != 'active':
            return None, '该挂单已成交或已下架'
        if listing.listing_type != 'barter':
            return None, '该挂单不是以物易物模式，不能出价'
        if listing.seller_adventurer_id == buyer_adv.id:
            return None, '不能对自己的挂单出价'

        offered, err = resolve_warehouse_item(
            buyer_adv.id, item_id=offered_warehouse_item_id, item_name=offered_item_name,
        )
        if err:
            return None, err

        offer = None
        try:
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers([
                    buyer_adv.id,
                    listing.seller_adventurer_id,
                ])
                S1MarketService._lock_warehouse_aggregates(locked_map)
                buyer_row = locked_map[buyer_adv.id]
                if buyer_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                buyer_can, buyer_reason = buyer_row.can_dispatch()
                if not buyer_can:
                    raise S1PlayerVisibleError(buyer_reason or '当前状态无法进行交易')

                offered_row = (
                    AdventurerS1WarehouseItem.query
                    .filter_by(id=offered.id, adventurer_id=buyer_row.id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not offered_row:
                    raise S1PlayerVisibleError(f'「{offered.name}」已不在你的仓库')
                tradeable_err = S1MarketService._check_tradeable_item(offered_row)
                if tradeable_err:
                    raise S1PlayerVisibleError(tradeable_err)

                listing_row = (
                    AdventurerS1MarketListing.query
                    .filter_by(id=listing_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not listing_row:
                    raise S1PlayerVisibleError('该挂单刚刚已被处理')
                if listing_row.listing_type != 'barter':
                    raise S1PlayerVisibleError('该挂单不是以物易物模式，不能出价')
                if listing_row.seller_adventurer_id == buyer_row.id:
                    raise S1PlayerVisibleError('不能对自己的挂单出价')
                clue_rarity_map = S1MarketService._clue_catalog_rarity_map({
                    offered_row.name,
                    listing_row.item_name,
                })
                offered_rarity = S1MarketService._effective_item_rarity(
                    offered_row.name, offered_row.rarity, clue_rarity_map,
                )
                listing_rarity = S1MarketService._effective_item_rarity(
                    listing_row.item_name, listing_row.rarity, clue_rarity_map,
                )
                listing_row.rarity = listing_rarity
                if offered_rarity != listing_rarity:
                    raise S1PlayerVisibleError('只能用同稀有度的物品出价')

                trade_qty, qty_err = S1MarketService._resolve_barter_trade_qty(
                    item_quantity_of(listing_row),
                    quantity,
                    item_quantity_of(offered_row),
                )
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                existing = AdventurerS1MarketOffer.query.filter_by(
                    listing_id=listing_id,
                    buyer_adventurer_id=buyer_row.id,
                    status='pending',
                ).first()
                if existing:
                    raise S1PlayerVisibleError('你已经对该挂单出过价了，请先取消再重新出价')

                offer = AdventurerS1MarketOffer(
                    listing_id=listing_id,
                    buyer_adventurer_id=buyer_row.id,
                    offered_item_name=offered_row.name,
                    offered_item_type=offered_row.type,
                    offered_rarity=offered_rarity,
                    offered_custom_desc=offered_row.custom_desc,
                    offered_quantity=trade_qty,
                    listing_quantity=trade_qty,
                    offered_uses_left=offered_row.uses_left,
                    offered_evolution_stage=(
                        max(0, int(offered_row.evolution_stage or 0))
                        if offered_row.type == 'companion' else None
                    ),
                    source_warehouse_item_id=offered_row.id,
                    status='pending',
                )
                ok, add_err = db_manager.add(offer)
                if not ok:
                    raise RuntimeError(add_err)
        except Exception as exc:
            return None, player_operation_failure(exc, '出价失败')

        S1MarketService._emit_market_changed('offer_submitted', listing_id)
        offer_dict = offer.to_dict()
        S1MarketService._finalize_market_client_payload(offer_dict)
        return offer_dict, None

    # ── 买家取消出价 ─────────────────────────────────────────────────────────

    @staticmethod
    def cancel_offer(user_id: int, offer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """买家取消自己 pending 状态的出价。"""
        offer = AdventurerS1MarketOffer.query.session.get(AdventurerS1MarketOffer, offer_id)
        if not offer:
            return None, '出价记录不存在'
        buyer_adv, err = S1MarketService._get_owned_adventurer(offer.buyer_adventurer_id, user_id)
        if err:
            return None, err
        if offer.status != 'pending':
            return None, '该出价已不是待处理状态'

        affected = AdventurerS1MarketOffer.query.filter_by(
            id=offer_id, status='pending',
        ).update({
            AdventurerS1MarketOffer.status: 'cancelled',
            AdventurerS1MarketOffer.updated_at: _portable_utc_now(),
        }, synchronize_session=False)
        if affected == 0:
            return None, '该出价刚刚已被处理'
        ok, commit_err = player_database_result(db_manager.commit())
        if not ok:
            return None, f'取消出价失败：{commit_err}'

        S1MarketService._emit_market_changed('offer_cancelled', offer.listing_id)
        return {'offer_id': offer_id, 'listing_id': offer.listing_id}, None

    @staticmethod
    def reject_offer(user_id: int, parent_id: int, offer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """挂单主人拒绝待处理报价；不移动资产，允许对方再次提交。"""
        parent = db_manager.query(AdventurerS1MarketListing).filter_by(id=parent_id).first()
        if not parent:
            return None, '挂单不存在'
        owner_id = parent.seller_adventurer_id
        try:
            with db_manager.transaction():
                owners = S1MarketService._lock_adventurers([owner_id])
                owner = owners.get(owner_id)
                if not owner or owner.owner_id != user_id:
                    raise S1PlayerVisibleError('只能处理自己挂单收到的报价')
                parent = (
                    db_manager.query(AdventurerS1MarketListing).filter_by(id=parent_id)
                    .populate_existing().with_for_update().first()
                )
                if not parent or parent.status != 'active' or parent.seller_adventurer_id != owner_id:
                    raise S1PlayerVisibleError('挂单已结束或归属已变化，请刷新后重试')
                offer = (
                    db_manager.query(AdventurerS1MarketOffer)
                    .filter_by(id=offer_id, listing_id=parent_id, status='pending')
                    .populate_existing().with_for_update().first()
                )
                if not offer:
                    raise S1PlayerVisibleError('报价已被处理或不属于该挂单')
                offer.status = 'rejected'
                offer.updated_at = _portable_utc_now()
        except Exception as exc:
            return None, player_operation_failure(exc, '拒绝报价失败')
        S1MarketService._emit_market_changed('offer_rejected', parent_id)
        return {'offer_id': offer_id, 'listing_id': parent_id}, None


    # ── 卖家查看出价 ─────────────────────────────────────────────────────────

    @staticmethod
    def get_offers_for_listing(user_id: int, listing_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """卖家查看某挂单的所有出价（含 pending / accepted / rejected / cancelled）。"""
        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing:
            return None, '挂单不存在'
        adv, err = S1MarketService._get_owned_adventurer(listing.seller_adventurer_id, user_id)
        if err:
            return None, err

        offers = (
            AdventurerS1MarketOffer.query
            .filter_by(listing_id=listing_id)
            .order_by(AdventurerS1MarketOffer.created_at.desc())
            .all()
        )
        buyer_ids = {o.buyer_adventurer_id for o in offers}
        name_map = S1MarketService._adv_name_map(buyer_ids)
        desc_map = S1MarketService._item_desc_map({o.offered_item_name for o in offers})

        listing_summary = {
            'id': listing.id, 'item_name': listing.item_name,
            'item_type': listing.item_type,
            'rarity': listing.rarity, 'status': listing.status,
            'quantity': int(listing.quantity or 1),
            'evolution_stage': max(0, int(listing.evolution_stage or 0)),
        }
        items = []
        for o in offers:
            d = o.to_dict(
                buyer_name=name_map.get(o.buyer_adventurer_id, '?'),
                listing_summary=listing_summary,
            )
            # 补充 desc
            d['offered_desc'] = o.offered_custom_desc or desc_map.get(o.offered_item_name, '')
            items.append(d)

        listing_dict = listing.to_dict(
            item_dict=desc_map, seller_name=adv.name, include_trade_info=True,
        )
        offers_payload = {
            'listing': listing_dict,
            'offers': items,
        }
        S1MarketService._finalize_market_client_payload(listing_dict, items)
        return offers_payload, None

    # ── 卖家选定出价 → 成交 ──────────────────────────────────────────────────

    @staticmethod
    def accept_offer(user_id: int, listing_id: int, offer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """卖家选定一个出价，按出价件数完成互换；部分成交后挂单可继续在售。"""
        listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, listing_id)
        if not listing or listing.status != 'active':
            return None, '该挂单已成交或已下架'
        adv, err = S1MarketService._get_owned_adventurer(listing.seller_adventurer_id, user_id)
        if err:
            return None, err

        offer = AdventurerS1MarketOffer.query.session.get(AdventurerS1MarketOffer, offer_id)
        if not offer or offer.listing_id != listing_id:
            return None, '出价不存在或不属于该挂单'
        if offer.status != 'pending':
            return None, '该出价已不是待处理状态'

        buyer_adv = AdventurerS1.query.session.get(AdventurerS1, offer.buyer_adventurer_id)
        if not buyer_adv:
            return None, '出价方冒险者不存在'

        trade_qty = S1MarketService._offer_listing_quantity(offer)
        if trade_qty > item_quantity_of(listing):
            return None, f'该挂单仅剩 {item_quantity_of(listing)} 件，无法按 {trade_qty} 件成交'

        if offer.source_warehouse_item_id is None:
            return None, '旧出价缺少物品实例编号，无法安全成交，请对方重新出价'
        wh_item = AdventurerS1WarehouseItem.query.session.get(AdventurerS1WarehouseItem, offer.source_warehouse_item_id)
        if not wh_item or wh_item.adventurer_id != buyer_adv.id:
            return None, '出价的原物品实例已不在对方仓库，无法成交'
        clue_rarity_map = S1MarketService._clue_catalog_rarity_map({
            listing.item_name,
            wh_item.name,
        })
        offered_rarity = S1MarketService._effective_item_rarity(
            wh_item.name, wh_item.rarity, clue_rarity_map,
        )
        listing_rarity = S1MarketService._effective_item_rarity(
            listing.item_name, listing.rarity, clue_rarity_map,
        )
        if not S1MarketService._barter_offer_snapshot_matches(
            offer,
            wh_item,
            effective_rarity=offered_rarity,
        ):
            return None, '出价的原物品实例属性或阶级已经变化，请对方重新出价'
        if item_quantity_of(wh_item) < trade_qty:
            return None, f'出价物品数量不足（需要 {trade_qty} 件，当前 {item_quantity_of(wh_item)} 件）'

        fee = S1MarketService._fee_for(listing_rarity)
        if fee > 0:
            buyer_balance, bal_err = S1CurrencyService.get_balance(buyer_adv.id, 'gold')
            if bal_err:
                return None, bal_err
            if buyer_balance is None or buyer_balance < fee - 0.001:
                return None, '对方金币不足以支付手续费，暂无法成交'

        try:
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers([
                    adv.id,
                    buyer_adv.id,
                ])
                S1MarketService._lock_warehouse_aggregates(locked_map)
                seller_row = locked_map[adv.id]
                buyer_row = locked_map[buyer_adv.id]
                if seller_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                seller_can, seller_reason = seller_row.can_dispatch()
                if not seller_can:
                    raise S1PlayerVisibleError(seller_reason or '当前状态无法进行交易')
                buyer_can, buyer_reason = buyer_row.can_dispatch()
                if not buyer_can:
                    raise S1PlayerVisibleError(buyer_reason or '出价者当前状态无法交易')
                wh_row = (
                    AdventurerS1WarehouseItem.query
                    .filter_by(id=wh_item.id, adventurer_id=buyer_adv.id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not wh_row:
                    raise S1PlayerVisibleError('出价物品已不在对方仓库')
                listing_row = (
                    AdventurerS1MarketListing.query
                    .filter_by(id=listing_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not listing_row:
                    raise S1PlayerVisibleError('该挂单刚刚已被处理')
                if listing_row.listing_type != 'barter':
                    raise S1PlayerVisibleError('该挂单不是以物易物模式')
                if listing_row.seller_adventurer_id != seller_row.id:
                    raise S1PlayerVisibleError('挂单方已变化，请刷新后重试')
                offer_row = (
                    AdventurerS1MarketOffer.query
                    .filter_by(id=offer_id, listing_id=listing_id, status='pending')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not offer_row:
                    raise S1PlayerVisibleError('该出价已不是待处理状态')
                if (
                    offer_row.buyer_adventurer_id != buyer_row.id
                    or offer_row.source_warehouse_item_id != wh_row.id
                ):
                    raise S1PlayerVisibleError('出价引用已变化，请刷新后重试')
                effective_offer_rarity = S1MarketService._effective_item_rarity(
                    wh_row.name,
                    wh_row.rarity,
                )
                if not S1MarketService._barter_offer_snapshot_matches(
                    offer_row,
                    wh_row,
                    effective_rarity=effective_offer_rarity,
                ):
                    raise S1PlayerVisibleError('出价物品属性或阶级已经变化，请重新出价')

                trade_qty = S1MarketService._offer_listing_quantity(offer_row)
                trade_qty, qty_err = S1MarketService._resolve_barter_trade_qty(
                    item_quantity_of(listing_row), trade_qty, item_quantity_of(wh_row),
                )
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)

                result = S1MarketService._execute_barter_trade(
                    listing_row, seller_row, buyer_row, wh_row, trade_qty, listing_id,
                )

                offer_row.status = 'accepted'
                offer_row.updated_at = _portable_utc_now()

                if result.get('listing_completed'):
                    AdventurerS1MarketOffer.query.filter(
                        AdventurerS1MarketOffer.listing_id == listing_id,
                        AdventurerS1MarketOffer.status == 'pending',
                        AdventurerS1MarketOffer.id != offer_id,
                    ).update({
                        AdventurerS1MarketOffer.status: 'rejected',
                        AdventurerS1MarketOffer.updated_at: _portable_utc_now(),
                    }, synchronize_session=False)
                else:
                    S1MarketService._reject_incompatible_pending_offers(
                        listing_id, item_quantity_of(listing_row), exclude_offer_id=offer_id,
                    )
        except Exception as e:
            return None, player_operation_failure(e, '成交失败')

        action = 'completed' if result.get('listing_completed') else 'partial_exchange'
        S1MarketService._emit_market_changed(action, listing_id)
        accept_payload = {'listing_id': listing_id, **result}
        S1MarketService._finalize_market_client_payload(accept_payload)
        return accept_payload, None

    # ── 买家查看自己的出价 ───────────────────────────────────────────────────

    @staticmethod
    def get_my_offers(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """买家查看自己所有出价（含关联挂单摘要）。"""
        adv_ids = [
            row[0] for row in
            AdventurerS1.query.with_entities(AdventurerS1.id)
            .filter_by(owner_id=user_id, is_player_created=True).all()
        ]
        if not adv_ids:
            return {'offers': []}, None

        offers = (
            AdventurerS1MarketOffer.query
            .filter(AdventurerS1MarketOffer.buyer_adventurer_id.in_(adv_ids))
            .order_by(AdventurerS1MarketOffer.created_at.desc())
            .limit(50).all()
        )
        if not offers:
            return {'offers': []}, None

        listing_ids = list({o.listing_id for o in offers})
        listings = AdventurerS1MarketListing.query.filter(
            AdventurerS1MarketListing.id.in_(listing_ids)
        ).all()
        listing_map = {l.id: l for l in listings}

        seller_ids = {l.seller_adventurer_id for l in listings}
        seller_name_map = S1MarketService._adv_name_map(seller_ids)

        items = []
        for o in offers:
            l = listing_map.get(o.listing_id)
            listing_summary = None
            if l:
                listing_summary = {
                    'id': l.id, 'item_name': l.item_name,
                    'item_type': l.item_type,
                    'rarity': l.rarity, 'status': l.status,
                    'evolution_stage': max(0, int(l.evolution_stage or 0)),
                    'seller_name': seller_name_map.get(l.seller_adventurer_id, '?'),
                }
            items.append(o.to_dict(listing_summary=listing_summary))

        S1MarketService._finalize_market_client_payload(items)
        return {'offers': items}, None


class S1MarketBuyOrderService:
    """个人交易市场求购：分稀有度定价、物品展示与求购者选定成交。"""

    _DEFAULT_MAX_ACCEPT_OFFERS = 100

    @staticmethod
    def _max_accept_offers() -> int:
        raw = current_app.config.get(
            'ADVENTURER_S1_MARKET_BUY_ORDER_MAX_ACCEPT_OFFERS',
            S1MarketBuyOrderService._DEFAULT_MAX_ACCEPT_OFFERS,
        )
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return S1MarketBuyOrderService._DEFAULT_MAX_ACCEPT_OFFERS

    @staticmethod
    def _normalize_offer_ids(offer_ids) -> Tuple[Optional[list[int]], Optional[str]]:
        if not isinstance(offer_ids, list) or not offer_ids:
            return None, 'offer_ids 必须是非空数组'
        max_offers = S1MarketBuyOrderService._max_accept_offers()
        if len(offer_ids) > max_offers:
            return None, f'一次最多选择 {max_offers} 个展示'

        normalized = []
        seen = set()
        for index, raw_offer_id in enumerate(offer_ids):
            if (
                isinstance(raw_offer_id, bool)
                or not isinstance(raw_offer_id, int)
                or raw_offer_id <= 0
            ):
                return None, f'offer_ids[{index}] 必须是正整数'
            if raw_offer_id in seen:
                return None, f'offer_ids[{index}] 与前面的展示重复'
            seen.add(raw_offer_id)
            normalized.append(raw_offer_id)
        return normalized, None

    @staticmethod
    def _normalize_prices(prices) -> Tuple[Optional[dict], Optional[str]]:
        if not isinstance(prices, dict):
            return None, 'prices 必须是按稀有度设置的价格对象'
        unknown = set(prices) - set(S1MarketService.VALID_RARITIES)
        if unknown:
            return None, f'包含无效的稀有度：{", ".join(sorted(unknown))}'

        normalized = {}
        for rarity in S1MarketService.VALID_RARITIES:
            raw = prices.get(rarity)
            if raw is None or raw == '':
                continue
            try:
                price = round(float(raw), 2)
            except (TypeError, ValueError):
                return None, f'{S1MarketService.RARITY_LABELS[rarity]}求购价格格式不正确'
            if not math.isfinite(price):
                return None, f'{S1MarketService.RARITY_LABELS[rarity]}求购价格格式不正确'
            if price <= 0:
                return None, f'{S1MarketService.RARITY_LABELS[rarity]}求购价格必须大于 0'
            price_err = S1MarketService._validate_sell_price(rarity, price, 1)
            if price_err:
                return None, price_err.replace('售价', '求购单价')
            normalized[rarity] = price
        if not normalized:
            return None, '至少设置一个稀有度的求购价格'
        return normalized, None

    @staticmethod
    def _price_for(order: AdventurerS1MarketBuyOrder, rarity: str) -> Optional[float]:
        if rarity not in S1MarketService.VALID_RARITIES:
            return None
        value = getattr(order, f'{rarity}_price', None)
        if value is None:
            return None
        try:
            price = round(float(value), 2)
        except (TypeError, ValueError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    @staticmethod
    def _apply_search(query, search: Optional[str]):
        keyword = S1MarketService._normalize_search(search)
        if not keyword:
            return query
        pattern = f'%{S1MarketService._escape_ilike_pattern(keyword)}%'
        buyer_name_matches = exists().where(and_(
            AdventurerS1.id == AdventurerS1MarketBuyOrder.buyer_adventurer_id,
            AdventurerS1.name.ilike(pattern, escape='\\'),
        ))
        return query.filter(or_(
            AdventurerS1MarketBuyOrder.note.ilike(pattern, escape='\\'),
            buyer_name_matches,
        ))

    @staticmethod
    def _pending_offer_counts_for_user(user_id: int) -> dict[int, int]:
        rows = (
            db_manager.query(
                AdventurerS1MarketBuyOffer.buy_order_id,
                func.count(AdventurerS1MarketBuyOffer.id),
            )
            .join(
                AdventurerS1MarketBuyOrder,
                AdventurerS1MarketBuyOrder.id == AdventurerS1MarketBuyOffer.buy_order_id,
            )
            .join(
                AdventurerS1,
                AdventurerS1.id == AdventurerS1MarketBuyOrder.buyer_adventurer_id,
            )
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1MarketBuyOrder.status == 'active',
                AdventurerS1MarketBuyOffer.status == 'pending',
            )
            .group_by(AdventurerS1MarketBuyOffer.buy_order_id)
            .all()
        )
        return {int(order_id): int(count) for order_id, count in rows}

    @staticmethod
    def _offer_dicts(
        offers: list[AdventurerS1MarketBuyOffer],
        *,
        order_map: Optional[dict[int, AdventurerS1MarketBuyOrder]] = None,
    ) -> list[dict]:
        if not offers:
            return []
        seller_names = S1MarketService._adv_name_map({row.seller_adventurer_id for row in offers})
        desc_map = S1MarketService._item_desc_map({row.item_name for row in offers})
        buyer_names = {}
        if order_map:
            buyer_names = S1MarketService._adv_name_map({
                order.buyer_adventurer_id for order in order_map.values()
            })

        items = []
        for offer in offers:
            order_summary = None
            if order_map:
                order = order_map.get(offer.buy_order_id)
                if order:
                    order_summary = {
                        'id': order.id,
                        'buyer_name': buyer_names.get(order.buyer_adventurer_id, '?'),
                        'note': order.note,
                        'prices': order.price_map(),
                        'status': order.status,
                    }
            item = offer.to_dict(
                seller_name=seller_names.get(offer.seller_adventurer_id, '?'),
                order_summary=order_summary,
            )
            if not item.get('desc'):
                item['desc'] = desc_map.get(offer.item_name, '')
            items.append(item)
        S1MarketService._finalize_market_client_payload(items)
        return items

    @staticmethod
    def create_order(
        user_id: int,
        adventurer_id: int,
        prices,
        note: Optional[str] = None,
        keep_open: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if not isinstance(keep_open, bool):
            return None, '持续求购选项必须为布尔值'
        err = S1MarketService._require_market_built()
        if err:
            return None, err
        buyer, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err
        normalized, err = S1MarketBuyOrderService._normalize_prices(prices)
        if err:
            return None, err

        note = (str(note).strip()[:200] or None) if note is not None else None
        order = AdventurerS1MarketBuyOrder(
            buyer_adventurer_id=buyer.id,
            note=note,
            common_price=normalized.get('common'),
            rare_price=normalized.get('rare'),
            epic_price=normalized.get('epic'),
            legendary_price=normalized.get('legendary'),
            keep_open=keep_open,
            status='active',
        )
        try:
            with db_manager.transaction():
                ok, add_err = db_manager.add(order)
                if not ok:
                    raise RuntimeError(add_err)
        except Exception as exc:
            return None, player_operation_failure(exc, '发布求购失败')

        S1MarketService._emit_market_changed('buy_order_created', order.id)
        return order.to_dict(buyer_name=buyer.name, include_trade_info=True), None

    @staticmethod
    def list_orders(
        page: int,
        per_page: int,
        rarity: Optional[str] = None,
        search: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        query = AdventurerS1MarketBuyOrder.query.filter_by(status='active')
        if rarity:
            if rarity not in S1MarketService.VALID_RARITIES:
                return None, '无效的稀有度'
            query = query.filter(getattr(AdventurerS1MarketBuyOrder, f'{rarity}_price').isnot(None))
        query = S1MarketBuyOrderService._apply_search(query, search)
        total = query.count()
        rows = (
            query.order_by(AdventurerS1MarketBuyOrder.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        buyer_names = S1MarketService._adv_name_map({row.buyer_adventurer_id for row in rows})
        items = [
            row.to_dict(buyer_name=buyer_names.get(row.buyer_adventurer_id, '?'))
            for row in rows
        ]
        pending_counts = (
            S1MarketBuyOrderService._pending_offer_counts_for_user(user_id)
            if user_id is not None else {}
        )
        for item in items:
            if item['id'] in pending_counts:
                item['offers_count'] = pending_counts[item['id']]

        data = build_paginated_response(items, total, page, per_page)
        data['my_pending_offer_count'] = sum(pending_counts.values())
        data['sell_price_range'] = S1MarketService._get_sell_price_range()
        data['sell_fee_rate'] = S1MarketService._get_sell_fee_rate()
        return data, None

    @staticmethod
    def get_my_orders(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        adv_ids = [
            row[0] for row in (
                AdventurerS1.query.with_entities(AdventurerS1.id)
                .filter_by(owner_id=user_id, is_player_created=True)
                .all()
            )
        ]
        if not adv_ids:
            return {'active': [], 'history': []}, None

        active_rows = (
            AdventurerS1MarketBuyOrder.query
            .filter(
                AdventurerS1MarketBuyOrder.buyer_adventurer_id.in_(adv_ids),
                AdventurerS1MarketBuyOrder.status == 'active',
            )
            .order_by(AdventurerS1MarketBuyOrder.created_at.desc())
            .all()
        )
        history_rows = (
            AdventurerS1MarketBuyOrder.query
            .filter(
                AdventurerS1MarketBuyOrder.buyer_adventurer_id.in_(adv_ids),
                AdventurerS1MarketBuyOrder.status != 'active',
            )
            .order_by(AdventurerS1MarketBuyOrder.updated_at.desc())
            .limit(20)
            .all()
        )
        completed_order_ids = {
            row.id for row in history_rows if row.status == 'completed'
        }
        accepted_rows = (
            AdventurerS1MarketBuyOffer.query
            .filter(
                AdventurerS1MarketBuyOffer.buy_order_id.in_(completed_order_ids),
                AdventurerS1MarketBuyOffer.status == 'accepted',
            )
            .order_by(
                AdventurerS1MarketBuyOffer.buy_order_id.asc(),
                AdventurerS1MarketBuyOffer.id.asc(),
            )
            .all()
            if completed_order_ids else []
        )
        accepted_dicts = S1MarketBuyOrderService._offer_dicts(accepted_rows)
        accepted_by_id = {item['id']: item for item in accepted_dicts}
        accepted_by_order: dict[int, list[dict]] = {}
        for item in accepted_dicts:
            accepted_by_order.setdefault(item['buy_order_id'], []).append(item)
        active_items = [
            row.to_dict(include_trade_info=True)
            for row in active_rows
        ]
        history_items = []
        for row in history_rows:
            accepted_offers = accepted_by_order.get(row.id, [])
            history_items.append(row.to_dict(
                include_trade_info=True,
                accepted_offer=(
                    accepted_by_id.get(row.accepted_offer_id)
                    or (accepted_offers[0] if accepted_offers else None)
                ),
                accepted_offers=accepted_offers,
            ))
        return {'active': active_items, 'history': history_items}, None

    @staticmethod
    def cancel_order(user_id: int, order_id: int) -> Tuple[Optional[dict], Optional[str]]:
        order = AdventurerS1MarketBuyOrder.query.session.get(AdventurerS1MarketBuyOrder, order_id)
        if not order:
            return None, '求购单不存在'
        _, err = S1MarketService._get_owned_adventurer(order.buyer_adventurer_id, user_id)
        if err:
            return None, err
        if order.status != 'active':
            return None, '该求购单已成交或已取消'

        now = _portable_utc_now()
        try:
            with db_manager.transaction():
                affected = AdventurerS1MarketBuyOrder.query.filter_by(
                    id=order_id,
                    status='active',
                ).update({
                    AdventurerS1MarketBuyOrder.status: 'cancelled',
                    AdventurerS1MarketBuyOrder.updated_at: now,
                }, synchronize_session=False)
                if affected == 0:
                    raise S1PlayerVisibleError('该求购单刚刚已被处理')
                AdventurerS1MarketBuyOffer.query.filter_by(
                    buy_order_id=order_id,
                    status='pending',
                ).update({
                    AdventurerS1MarketBuyOffer.status: 'rejected',
                    AdventurerS1MarketBuyOffer.updated_at: now,
                }, synchronize_session=False)
        except Exception as exc:
            return None, player_operation_failure(exc, '取消求购失败')

        S1MarketService._emit_market_changed('buy_order_cancelled', order_id)
        return {'buy_order_id': order_id}, None

    @staticmethod
    def submit_offer(
        user_id: int,
        adventurer_id: int,
        order_id: int,
        warehouse_item_id: int,
        quantity: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            resolve_move_quantity,
            resolve_warehouse_item,
        )

        err = S1MarketService._require_market_built()
        if err:
            return None, err
        seller, err = S1MarketService._get_owned_adventurer(adventurer_id, user_id)
        if err:
            return None, err
        order = AdventurerS1MarketBuyOrder.query.session.get(AdventurerS1MarketBuyOrder, order_id)
        if not order or order.status != 'active':
            return None, '该求购单已成交或已取消'
        if order.buyer_adventurer_id == seller.id:
            return None, '不能向自己的求购单展示物品'

        item, err = resolve_warehouse_item(seller.id, item_id=warehouse_item_id)
        if err:
            return None, err

        offer = None
        try:
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers([
                    seller.id,
                    order.buyer_adventurer_id,
                ])
                S1MarketService._lock_warehouse_aggregates(locked_map)
                seller_row = locked_map[seller.id]
                if seller_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                seller_can, seller_reason = seller_row.can_dispatch()
                if not seller_can:
                    raise S1PlayerVisibleError(seller_reason or '当前状态无法进行交易')

                item_row = (
                    AdventurerS1WarehouseItem.query
                    .filter_by(id=item.id, adventurer_id=seller_row.id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not item_row:
                    raise S1PlayerVisibleError(f'「{item.name}」已不在你的仓库')
                tradeable_err = S1MarketService._check_tradeable_item(item_row)
                if tradeable_err:
                    raise S1PlayerVisibleError(tradeable_err)

                order_row = (
                    AdventurerS1MarketBuyOrder.query
                    .filter_by(id=order_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not order_row:
                    raise S1PlayerVisibleError('该求购单刚刚已被处理')
                if order_row.buyer_adventurer_id == seller_row.id:
                    raise S1PlayerVisibleError('不能向自己的求购单展示物品')
                if order_row.buyer_adventurer_id not in locked_map:
                    raise S1PlayerVisibleError('求购者已变化，请刷新后重试')
                effective_rarity = S1MarketService._effective_item_rarity(
                    item_row.name,
                    item_row.rarity,
                )
                if S1MarketBuyOrderService._price_for(order_row, effective_rarity) is None:
                    raise S1PlayerVisibleError('该求购单没有设置此稀有度的价格')
                pending_offer_count = AdventurerS1MarketBuyOffer.query.filter_by(
                    buy_order_id=order_id,
                    status='pending',
                ).count()
                if pending_offer_count >= S1MarketBuyOrderService._max_accept_offers():
                    return None, '该求购单待处理展示已满，请等待求购者处理'
                offer_qty, qty_err = resolve_move_quantity(item_row, quantity)
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                duplicate = AdventurerS1MarketBuyOffer.query.filter_by(
                    buy_order_id=order_id,
                    source_warehouse_item_id=item_row.id,
                    status='pending',
                ).first()
                if duplicate:
                    raise S1PlayerVisibleError('该物品已经展示给这位求购者')
                offer = AdventurerS1MarketBuyOffer(
                    buy_order_id=order_id,
                    seller_adventurer_id=seller_row.id,
                    source_warehouse_item_id=item_row.id,
                    item_name=item_row.name,
                    item_type=item_row.type,
                    rarity=effective_rarity,
                    custom_desc=item_row.custom_desc,
                    quantity=offer_qty,
                    uses_left=item_row.uses_left,
                    evolution_stage=(
                        max(0, int(item_row.evolution_stage or 0))
                        if item_row.type == 'companion' else None
                    ),
                    status='pending',
                )
                ok, add_err = db_manager.add(offer)
                if not ok:
                    raise RuntimeError(add_err)
        except Exception as exc:
            return None, player_operation_failure(exc, '展示失败')

        S1MarketService._emit_market_changed('buy_offer_submitted', order_id)
        offer_dict = S1MarketBuyOrderService._offer_dicts([offer])[0]
        return offer_dict, None

    @staticmethod
    def get_offers_for_order(
        user_id: int,
        order_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        order = AdventurerS1MarketBuyOrder.query.session.get(AdventurerS1MarketBuyOrder, order_id)
        if not order:
            return None, '求购单不存在'
        buyer, err = S1MarketService._get_owned_adventurer(order.buyer_adventurer_id, user_id)
        if err:
            return None, err
        pending_offers = (
            AdventurerS1MarketBuyOffer.query
            .filter_by(buy_order_id=order_id, status='pending')
            .order_by(AdventurerS1MarketBuyOffer.created_at.desc())
            .all()
        )
        history_limit = max(
            0,
            S1MarketBuyOrderService._max_accept_offers() - len(pending_offers),
        )
        history_offers = (
            AdventurerS1MarketBuyOffer.query
            .filter(
                AdventurerS1MarketBuyOffer.buy_order_id == order_id,
                AdventurerS1MarketBuyOffer.status != 'pending',
            )
            .order_by(AdventurerS1MarketBuyOffer.created_at.desc())
            .limit(history_limit)
            .all()
            if history_limit
            else []
        )
        offers = [*pending_offers, *history_offers]
        return {
            'buy_order': order.to_dict(
                buyer_name=buyer.name,
                include_trade_info=True,
            ),
            'offers': S1MarketBuyOrderService._offer_dicts(offers),
        }, None

    @staticmethod
    def cancel_offer(user_id: int, offer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        offer = AdventurerS1MarketBuyOffer.query.session.get(AdventurerS1MarketBuyOffer, offer_id)
        if not offer:
            return None, '展示记录不存在'
        _, err = S1MarketService._get_owned_adventurer(offer.seller_adventurer_id, user_id)
        if err:
            return None, err
        if offer.status != 'pending':
            return None, '该展示已不是待处理状态'

        affected = AdventurerS1MarketBuyOffer.query.filter_by(
            id=offer_id,
            status='pending',
        ).update({
            AdventurerS1MarketBuyOffer.status: 'cancelled',
            AdventurerS1MarketBuyOffer.updated_at: _portable_utc_now(),
        }, synchronize_session=False)
        if affected == 0:
            return None, '该展示刚刚已被处理'
        ok, commit_err = player_database_result(db_manager.commit())
        if not ok:
            return None, f'取消展示失败：{commit_err}'

        S1MarketService._emit_market_changed('buy_offer_cancelled', offer.buy_order_id)
        return {'offer_id': offer_id, 'buy_order_id': offer.buy_order_id}, None

    @staticmethod
    def reject_offer(user_id: int, parent_id: int, offer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """挂单主人拒绝待处理报价；不移动资产，允许对方再次提交。"""
        parent = db_manager.query(AdventurerS1MarketBuyOrder).filter_by(id=parent_id).first()
        if not parent:
            return None, '挂单不存在'
        owner_id = parent.buyer_adventurer_id
        try:
            with db_manager.transaction():
                owners = S1MarketService._lock_adventurers([owner_id])
                owner = owners.get(owner_id)
                if not owner or owner.owner_id != user_id:
                    raise S1PlayerVisibleError('只能处理自己挂单收到的报价')
                parent = (
                    db_manager.query(AdventurerS1MarketBuyOrder).filter_by(id=parent_id)
                    .populate_existing().with_for_update().first()
                )
                if not parent or parent.status != 'active' or parent.buyer_adventurer_id != owner_id:
                    raise S1PlayerVisibleError('挂单已结束或归属已变化，请刷新后重试')
                offer = (
                    db_manager.query(AdventurerS1MarketBuyOffer)
                    .filter_by(id=offer_id, buy_order_id=parent_id, status='pending')
                    .populate_existing().with_for_update().first()
                )
                if not offer:
                    raise S1PlayerVisibleError('报价已被处理或不属于该挂单')
                offer.status = 'rejected'
                offer.updated_at = _portable_utc_now()
        except Exception as exc:
            return None, player_operation_failure(exc, '拒绝报价失败')
        S1MarketService._emit_market_changed('buy_offer_rejected', parent_id)
        return {'offer_id': offer_id, 'buy_order_id': parent_id}, None


    @staticmethod
    def get_my_offers(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        adv_ids = [
            row[0] for row in (
                AdventurerS1.query.with_entities(AdventurerS1.id)
                .filter_by(owner_id=user_id, is_player_created=True)
                .all()
            )
        ]
        if not adv_ids:
            return {'offers': []}, None
        pending_offers = (
            AdventurerS1MarketBuyOffer.query
            .filter(
                AdventurerS1MarketBuyOffer.seller_adventurer_id.in_(adv_ids),
                AdventurerS1MarketBuyOffer.status == 'pending',
            )
            .order_by(AdventurerS1MarketBuyOffer.created_at.desc())
            .all()
        )
        history_offers = (
            AdventurerS1MarketBuyOffer.query
            .filter(
                AdventurerS1MarketBuyOffer.seller_adventurer_id.in_(adv_ids),
                AdventurerS1MarketBuyOffer.status != 'pending',
            )
            .order_by(AdventurerS1MarketBuyOffer.created_at.desc())
            .limit(50)
            .all()
        )
        offers = [*pending_offers, *history_offers]
        order_ids = {offer.buy_order_id for offer in offers}
        orders = (
            AdventurerS1MarketBuyOrder.query
            .filter(AdventurerS1MarketBuyOrder.id.in_(order_ids))
            .all()
            if order_ids else []
        )
        order_map = {order.id: order for order in orders}
        return {
            'offers': S1MarketBuyOrderService._offer_dicts(
                offers,
                order_map=order_map,
            ),
        }, None

    @staticmethod
    def _snapshot_matches(
        offer: AdventurerS1MarketBuyOffer,
        item: AdventurerS1WarehouseItem,
        *,
        effective_rarity: Optional[str] = None,
    ) -> bool:
        stage_matches = (
            offer.evolution_stage is None
            or max(0, int(offer.evolution_stage)) == max(
                0, int(item.evolution_stage or 0),
            )
        )
        return (
            offer.item_name == item.name
            and (offer.item_type or '') == (item.type or '')
            and offer.rarity == (effective_rarity or item.rarity)
            and (offer.custom_desc or '') == (item.custom_desc or '')
            and offer.uses_left == item.uses_left
            and stage_matches
        )

    @staticmethod
    def accept_offers(
        user_id: int,
        order_id: int,
        offer_ids,
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.s1_item_instance_service import take_from_stack

        normalized_offer_ids, err = S1MarketBuyOrderService._normalize_offer_ids(offer_ids)
        if err:
            return None, err
        err = S1MarketService._require_market_built()
        if err:
            return None, err

        order_projection = (
            db_manager.query(
                AdventurerS1MarketBuyOrder.id,
                AdventurerS1MarketBuyOrder.buyer_adventurer_id,
                AdventurerS1MarketBuyOrder.status,
            )
            .filter(AdventurerS1MarketBuyOrder.id == order_id)
            .first()
        )
        if not order_projection or order_projection.status != 'active':
            return None, '该求购单已成交或已取消'

        offer_projections = (
            db_manager.query(
                AdventurerS1MarketBuyOffer.id,
                AdventurerS1MarketBuyOffer.buy_order_id,
                AdventurerS1MarketBuyOffer.seller_adventurer_id,
                AdventurerS1MarketBuyOffer.source_warehouse_item_id,
            )
            .filter(AdventurerS1MarketBuyOffer.id.in_(normalized_offer_ids))
            .all()
        )
        projection_map = {int(row.id): row for row in offer_projections}
        if len(projection_map) != len(normalized_offer_ids):
            return None, '部分展示不存在或已被删除'
        for offer_id in normalized_offer_ids:
            if int(projection_map[offer_id].buy_order_id) != order_id:
                return None, '展示不存在或不属于该求购单'

        buyer_id = int(order_projection.buyer_adventurer_id)
        seller_ids = {
            int(projection_map[offer_id].seller_adventurer_id)
            for offer_id in normalized_offer_ids
        }
        source_item_ids = {
            int(projection_map[offer_id].source_warehouse_item_id)
            for offer_id in normalized_offer_ids
        }
        if len(source_item_ids) != len(normalized_offer_ids):
            return None, '所选展示包含重复的来源物品，请刷新后重试'

        participant_ids = sorted({buyer_id, *seller_ids})
        selected_offer_ids = set(normalized_offer_ids)

        fee_rate = S1MarketService._get_sell_fee_rate()
        accepted_offers: list[dict] = []
        line_results: list[dict] = []
        total_price = 0.0
        seller_income = 0.0
        market_fee = 0.0
        quantity_total = 0
        order_kept_open = False

        @db_retry(retry_integrity_constraints=('uq_advs1_currency',))
        def settle_once():
            nonlocal accepted_offers, line_results
            nonlocal total_price, seller_income, market_fee, quantity_total
            nonlocal order_kept_open
            accepted_offers = []
            line_results = []
            total_price = 0.0
            seller_income = 0.0
            market_fee = 0.0
            quantity_total = 0
            order_kept_open = False
            with db_manager.transaction():
                locked_map = S1MarketService._lock_adventurers(participant_ids)
                S1MarketService._lock_warehouse_aggregates(participant_ids)
                buyer_row = locked_map[buyer_id]
                if buyer_row.owner_id != user_id:
                    raise S1PlayerVisibleError('无权操作此冒险者')
                buyer_can, buyer_reason = buyer_row.can_dispatch()
                if not buyer_can:
                    raise S1PlayerVisibleError(buyer_reason or '求购者当前状态无法交易')
                for seller_id in sorted(seller_ids):
                    if seller_id == buyer_id:
                        raise S1PlayerVisibleError('不能购买自己展示的物品')
                    seller_can, seller_reason = locked_map[seller_id].can_dispatch()
                    if not seller_can:
                        raise S1PlayerVisibleError(seller_reason or '展示者当前状态无法交易')

                item_rows = (
                    AdventurerS1WarehouseItem.query
                    .filter(AdventurerS1WarehouseItem.id.in_(sorted(source_item_ids)))
                    .order_by(AdventurerS1WarehouseItem.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                item_map = {row.id: row for row in item_rows}
                if len(item_map) != len(source_item_ids):
                    raise S1PlayerVisibleError('部分展示物品已不在对方仓库')

                from plugins.adventurer_s1.services.s1_item_grant_service import (
                    prepare_warehouse_grant_batch,
                )

                warehouse_grant_context = prepare_warehouse_grant_batch(
                    buyer_row,
                    [row.name for row in item_rows],
                )
                from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                    is_hidden_boss_clue_item,
                )

                clue_rarity_map = {
                    name: str(catalog_row.rarity or 'common')
                    for name, catalog_row in warehouse_grant_context.catalog_by_name.items()
                    if is_hidden_boss_clue_item(catalog_row.use_condition)
                }

                order_row = (
                    AdventurerS1MarketBuyOrder.query
                    .filter_by(id=order_id, status='active')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not order_row:
                    raise S1PlayerVisibleError('该求购单刚刚已被处理')

                affected_offer_rows = (
                    AdventurerS1MarketBuyOffer.query
                    .filter(
                        AdventurerS1MarketBuyOffer.status == 'pending',
                        or_(
                            AdventurerS1MarketBuyOffer.buy_order_id == order_id,
                            AdventurerS1MarketBuyOffer.source_warehouse_item_id.in_(
                                source_item_ids,
                            ),
                        ),
                    )
                    .order_by(AdventurerS1MarketBuyOffer.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                offer_map = {
                    row.id: row
                    for row in affected_offer_rows
                    if row.id in selected_offer_ids and row.buy_order_id == order_id
                }
                if len(offer_map) != len(normalized_offer_ids):
                    raise S1PlayerVisibleError('部分展示已不是待处理状态，请刷新后重试')

                line_results = []
                seller_credit_entries = []
                for offer_id in normalized_offer_ids:
                    offer_row = offer_map[offer_id]
                    seller_row = locked_map.get(offer_row.seller_adventurer_id)
                    item_row = item_map.get(offer_row.source_warehouse_item_id)
                    if not seller_row or offer_row.seller_adventurer_id == buyer_id:
                        raise S1PlayerVisibleError('展示方冒险者已变化，请刷新后重试')
                    if not item_row or item_row.adventurer_id != seller_row.id:
                        raise S1PlayerVisibleError('展示物品已不在对方仓库')
                    tradeable_err = S1MarketService._check_tradeable_item(item_row)
                    if tradeable_err:
                        raise S1PlayerVisibleError(tradeable_err)
                    effective_rarity = S1MarketService._effective_item_rarity(
                        item_row.name,
                        item_row.rarity,
                        clue_rarity_map,
                    )
                    if not S1MarketBuyOrderService._snapshot_matches(
                        offer_row,
                        item_row,
                        effective_rarity=effective_rarity,
                    ):
                        raise S1PlayerVisibleError('展示物品信息已变化，请重新展示')

                    quantity = int(offer_row.quantity or 0)
                    if quantity <= 0:
                        raise S1PlayerVisibleError('展示物品数量异常，请重新展示')
                    current_quantity = item_quantity_of(item_row)
                    if current_quantity < quantity:
                        raise S1PlayerVisibleError(
                            '展示物品数量不足'
                            f'（需要 {quantity} 件，当前 {current_quantity} 件）'
                        )
                    unit_price = S1MarketBuyOrderService._price_for(
                        order_row,
                        effective_rarity,
                    )
                    if unit_price is None:
                        raise S1PlayerVisibleError('该求购单没有设置此稀有度的价格')
                    price_err = S1MarketService._validate_sell_price(
                        effective_rarity,
                        unit_price,
                        1,
                    )
                    if price_err:
                        raise S1PlayerVisibleError(price_err.replace('售价', '求购单价'))

                    line_price = round(unit_price * quantity, 2)
                    line_income = round(line_price * (1 - fee_rate), 2)
                    line_fee = round(line_price - line_income, 2)
                    rarity_label = S1MarketService.RARITY_LABELS.get(
                        effective_rarity,
                        effective_rarity,
                    )
                    qty_note = f' ×{quantity}' if quantity > 1 else ''
                    seller_credit_entries.append({
                        'adventurer_id': seller_row.id,
                        'change_amount': line_income,
                        'source_type': 'market_buy_order_sale',
                        'related_id': order_id,
                        'related_type': 'market_buy_order',
                        'description': (
                            f'交易市场响应求购售出「{offer_row.item_name}」{qty_note}'
                            f'（{rarity_label}，手续费 {line_fee}）'
                        ),
                    })
                    line_results.append({
                        'offer_row': offer_row,
                        'item_row': item_row,
                        'quantity': quantity,
                        'remaining_quantity': current_quantity - quantity,
                        'unit_price': unit_price,
                        'price_paid': line_price,
                        'seller_income': line_income,
                        'market_fee': line_fee,
                        'rarity_label': rarity_label,
                        'effective_rarity': effective_rarity,
                    })

                total_price = round(sum(line['price_paid'] for line in line_results), 2)
                seller_income = round(sum(line['seller_income'] for line in line_results), 2)
                market_fee = round(sum(line['market_fee'] for line in line_results), 2)
                quantity_total = sum(line['quantity'] for line in line_results)

                currency_entries = [{
                    'adventurer_id': buyer_row.id,
                    'change_amount': -total_price,
                    'source_type': 'market_buy_order_purchase',
                    'related_id': order_id,
                    'related_type': 'market_buy_order',
                    'description': (
                        f'交易市场求购成交 {len(line_results)} 项展示'
                        f'（共 {quantity_total} 件）'
                    ),
                    'insufficient_error': f'金币不足（需要 {total_price} 金币）',
                }, *seller_credit_entries]
                _, currency_err = S1CurrencyService.apply_changes_in_transaction(
                    'gold',
                    currency_entries,
                )
                if currency_err:
                    raise S1PlayerVisibleError(currency_err)

                for line in line_results:
                    offer_row = line['offer_row']
                    item_row = line['item_row']
                    take_from_stack(item_row, line['quantity'])
                    S1MarketService._log_market_movement(
                        offer_row.seller_adventurer_id,
                        name=offer_row.item_name,
                        item_type=offer_row.item_type,
                        rarity=line['effective_rarity'],
                        quantity=line['quantity'],
                        change_type='consume',
                        listing_id=order_id,
                        related_type='market_buy_order',
                        description=f'响应求购售出（{line["rarity_label"]}）',
                    )

                flush_ok, flush_err = db_manager.flush()
                if not flush_ok:
                    raise RuntimeError(flush_err or '展示物品移出失败')

                for line in line_results:
                    offer_row = line['offer_row']
                    item_row = line['item_row']
                    S1MarketService._deliver_item_to_warehouse(
                        buyer_row.id,
                        name=offer_row.item_name,
                        item_type=offer_row.item_type,
                        rarity=line['effective_rarity'],
                        custom_desc=offer_row.custom_desc,
                        uses_left=offer_row.uses_left,
                        quantity=line['quantity'],
                        evolution_stage=item_row.evolution_stage,
                        recovery_minutes=item_row.recovery_minutes,
                        recovery_started_at=item_row.recovery_started_at,
                        recovery_ends_at=item_row.recovery_ends_at,
                        listing_id=order_id,
                        description=f'交易市场求购入库（{line["rarity_label"]}）',
                        related_type='market_buy_order',
                        adventurer=buyer_row,
                        warehouse_batch_context=warehouse_grant_context,
                    )

                now = _portable_utc_now()
                order_kept_open = bool(order_row.keep_open)
                if order_kept_open:
                    if order_row.accepted_offer_id is None:
                        order_row.accepted_offer_id = normalized_offer_ids[0]
                    order_row.price_paid = round(
                        float(order_row.price_paid or 0) + total_price,
                        2,
                    )
                    order_row.seller_income = round(
                        float(order_row.seller_income or 0) + seller_income,
                        2,
                    )
                    order_row.market_fee = round(
                        float(order_row.market_fee or 0) + market_fee,
                        2,
                    )
                else:
                    order_row.status = 'completed'
                    order_row.accepted_offer_id = normalized_offer_ids[0]
                    order_row.price_paid = total_price
                    order_row.seller_income = seller_income
                    order_row.market_fee = market_fee
                    order_row.completed_at = now
                order_row.updated_at = now
                for offer_id in normalized_offer_ids:
                    offer_row = offer_map[offer_id]
                    offer_row.status = 'accepted'
                    offer_row.updated_at = now

                remaining_quantity_by_source_id = {
                    line['offer_row'].source_warehouse_item_id: line['remaining_quantity']
                    for line in line_results
                }
                for affected_offer in affected_offer_rows:
                    if affected_offer.id in selected_offer_ids:
                        continue
                    if affected_offer.buy_order_id == order_id:
                        if order_kept_open:
                            continue
                    else:
                        remaining_quantity = remaining_quantity_by_source_id.get(
                            affected_offer.source_warehouse_item_id,
                            0,
                        )
                        offered_quantity = int(affected_offer.quantity or 0)
                        if 0 < offered_quantity <= remaining_quantity:
                            continue
                    affected_offer.status = 'rejected'
                    affected_offer.updated_at = now

                desc_map = S1MarketService._item_desc_map({
                    offer_map[offer_id].item_name for offer_id in normalized_offer_ids
                })
                line_by_offer_id = {
                    line['offer_row'].id: line
                    for line in line_results
                }
                accepted_offers = []
                for offer_id in normalized_offer_ids:
                    offer_row = offer_map[offer_id]
                    accepted_offer = offer_row.to_dict(
                        seller_name=locked_map[offer_row.seller_adventurer_id].name,
                    )
                    if not accepted_offer.get('desc'):
                        accepted_offer['desc'] = desc_map.get(offer_row.item_name, '')
                    line = line_by_offer_id[offer_id]
                    accepted_offer.update({
                        'unit_price': line['unit_price'],
                        'price_paid': line['price_paid'],
                        'seller_income': line['seller_income'],
                        'market_fee': line['market_fee'],
                    })
                    accepted_offers.append(accepted_offer)
        try:
            settle_once()
        except Exception as exc:
            return None, player_operation_failure(exc, '求购成交失败')

        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
        )
        for adventurer_id in participant_ids:
            invalidate_adventurer_detail_cache(adventurer_id)
        market_event = (
            'buy_order_trade_completed'
            if order_kept_open else 'buy_order_completed'
        )
        S1MarketService._emit_market_changed(market_event, order_id)
        S1MarketService._emit_adventurers_changed(
            (
                'market_buy_order_trade_completed'
                if order_kept_open else 'market_buy_order_completed'
            ),
            set(participant_ids),
        )

        S1MarketService._finalize_market_client_payload(accepted_offers)
        result = {
            'buy_order_id': order_id,
            'offer_ids': list(normalized_offer_ids),
            'offer_count': len(normalized_offer_ids),
            'quantity_total': quantity_total,
            'accepted_offers': accepted_offers,
            'price_paid': total_price,
            'seller_income': seller_income,
            'market_fee': market_fee,
            'keep_open': order_kept_open,
            'buy_order_status': 'active' if order_kept_open else 'completed',
        }
        S1MarketService._finalize_market_client_payload(result)
        return result, None

    @staticmethod
    def accept_offer(
        user_id: int,
        order_id: int,
        offer_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        result, err = S1MarketBuyOrderService.accept_offers(
            user_id,
            order_id,
            [offer_id],
        )
        if err or not result:
            return result, err
        accepted_offer = result['accepted_offers'][0]
        result.update({
            'offer_id': accepted_offer['id'],
            'item_name': accepted_offer['item_name'],
            'rarity': accepted_offer['rarity'],
            'quantity': accepted_offer['quantity'],
        })
        return result, None

    @staticmethod
    def cancel_adventurer_activity(adventurer_id: int) -> int:
        """死亡或归档时撤销该冒险者的求购单及所有待处理展示。"""
        now = _portable_utc_now()
        order_ids = [
            row[0] for row in (
                AdventurerS1MarketBuyOrder.query
                .with_entities(AdventurerS1MarketBuyOrder.id)
                .filter(
                    AdventurerS1MarketBuyOrder.buyer_adventurer_id == adventurer_id,
                    AdventurerS1MarketBuyOrder.status == 'active',
                )
                .all()
            )
        ]
        changed = 0
        if order_ids:
            changed += AdventurerS1MarketBuyOrder.query.filter(
                AdventurerS1MarketBuyOrder.id.in_(order_ids),
                AdventurerS1MarketBuyOrder.status == 'active',
            ).update({
                AdventurerS1MarketBuyOrder.status: 'cancelled',
                AdventurerS1MarketBuyOrder.updated_at: now,
            }, synchronize_session=False)
            AdventurerS1MarketBuyOffer.query.filter(
                AdventurerS1MarketBuyOffer.buy_order_id.in_(order_ids),
                AdventurerS1MarketBuyOffer.status == 'pending',
            ).update({
                AdventurerS1MarketBuyOffer.status: 'rejected',
                AdventurerS1MarketBuyOffer.updated_at: now,
            }, synchronize_session=False)
        changed += AdventurerS1MarketBuyOffer.query.filter(
            AdventurerS1MarketBuyOffer.seller_adventurer_id == adventurer_id,
            AdventurerS1MarketBuyOffer.status == 'pending',
        ).update({
            AdventurerS1MarketBuyOffer.status: 'cancelled',
            AdventurerS1MarketBuyOffer.updated_at: now,
        }, synchronize_session=False)
        return int(changed or 0)
