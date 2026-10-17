"""管理员历史漏扣转职凭证回收。

只用探险出发快照、结构化职业变化与图鉴使用条件精确匹配；不读取剧情正文或调用 AI。
预览负责精确条件匹配与流向追踪，执行阶段在单事务内重新锁定并校验物品、挂单和金币。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
import hashlib
from flask import current_app
from sqlalchemy import String, and_, cast, or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BugReclaimBatch,
    AdventurerS1BugReclaimItem,
    AdventurerS1CurrencyLog,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1Item,
    AdventurerS1ItemLog,
    AdventurerS1MarketListing,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_item_instance_service import consume_item_stack
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    class_transfer_invitation_matches_dungeon,
    is_class_transfer_invitation,
    normalize_use_condition,
)


_SNAPSHOT_OPERATION = 'class_scroll_reclaims'
_MAX_PREVIEW_ITEMS = 50


class S1ClassScrollReclaimService:
    """预览并执行历史转职凭证漏扣回收。"""

    @staticmethod
    def _name(value: object) -> str:
        return str(value or '').strip()

    @classmethod
    def _missing_item_charge(cls, rarity: object) -> float:
        prices = current_app.config.get(
            'ADVENTURER_S1_CLASS_SCROLL_MISSING_ITEM_CHARGES',
            {},
        )
        if not isinstance(prices, dict):
            return 0.0
        try:
            return max(0.0, round(float(prices.get(cls._name(rarity).lower()) or 0), 2))
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _consumed_names(cls, update: dict) -> set[str]:
        names: set[str] = set()
        for row in update.get('consumed_equipments') or []:
            if isinstance(row, dict):
                name = cls._name(row.get('name'))
            else:
                name = cls._name(row)
            if name:
                names.add(name)
        return names

    @classmethod
    def _collect_structured_signals(cls) -> tuple[list[dict], dict]:
        """机械筛选显式 new_class 与未扣卷轴；重复转成同职业也属于转职。"""
        text = cast(AdventurerS1ExpeditionLog.full_log, String)
        try:
            page_size = max(1, int(current_app.config.get(
                'ADVENTURER_S1_CLASS_SCROLL_RECLAIM_SCAN_PAGE_SIZE',
                500,
            )))
        except (TypeError, ValueError):
            page_size = 500
        query = (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.party_id,
                AdventurerS1ExpeditionLog.target_dungeon,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.created_at,
            )
            .filter(
                AdventurerS1ExpeditionLog.status == 'completed',
                or_(
                    AdventurerS1ExpeditionLog.cold_compressed_at.isnot(None),
                    and_(
                        AdventurerS1ExpeditionLog.cold_compressed_at.is_(None),
                        text.contains('"new_class"'),
                        text.contains('"audit_member_snapshot"'),
                    ),
                ),
            )
            .order_by(AdventurerS1ExpeditionLog.id.desc())
        )
        # 展示上限只能限制返回行数，不能裁剪证据扫描。这里用键集分页走到
        # 查询耗尽；否则随着新日志增长，旧漏扣会永久滑出固定窗口并被误报为 0 项。
        processed = {
            row[0]
            for row in db_manager.query(AdventurerS1BugReclaimItem.issue_key).filter(
                AdventurerS1BugReclaimItem.issue_type == 'class_scroll_missing_consumption',
                AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
            ).all()
        }
        signals: list[dict] = []
        logs_scanned = 0
        class_field_conflicts = 0

        def iter_log_rows():
            before_id: int | None = None
            while True:
                page_query = query
                if before_id is not None:
                    page_query = page_query.filter(AdventurerS1ExpeditionLog.id < before_id)
                rows = page_query.limit(page_size).all()
                db_manager.rollback()
                if not rows:
                    return
                yield from rows
                before_id = int(rows[-1][0])

        for log_id, party_id, target_dungeon, full_log, created_at in iter_log_rows():
            logs_scanned += 1
            if not isinstance(full_log, dict):
                continue
            snapshots = {
                int(row.get('adventurer_id')): row
                for row in (full_log.get('audit_member_snapshot') or [])
                if isinstance(row, dict) and str(row.get('adventurer_id') or '').isdigit()
            }
            for update in full_log.get('member_updates') or []:
                if not isinstance(update, dict):
                    continue
                try:
                    adventurer_id = int(update.get('id'))
                except (TypeError, ValueError):
                    continue
                snapshot = snapshots.get(adventurer_id)
                new_class = cls._name(update.get('new_class'))
                if not snapshot or not new_class:
                    continue
                old_class = cls._name(snapshot.get('character_class'))
                consumed = cls._consumed_names(update)
                equipment = [
                    item for item in (snapshot.get('equipment') or [])
                    if isinstance(item, dict)
                    and cls._name(item.get('name'))
                    and cls._name(item.get('type')).lower() == 'scroll'
                    and cls._name(item.get('name')) not in consumed
                ]
                if not equipment:
                    continue
                prefix = f'class_scroll:{int(log_id)}:{adventurer_id}:'
                if any(key.startswith(prefix) for key in processed):
                    continue
                signals.append({
                    'log_id': int(log_id),
                    'party_id': int(party_id or 0),
                    'target_dungeon': cls._name(target_dungeon),
                    'created_at': created_at,
                    'adventurer_id': adventurer_id,
                    'old_class': old_class,
                    'new_class': new_class,
                    'update': {
                        'id': adventurer_id,
                        'new_class': new_class,
                        'consumed_equipments': list(update.get('consumed_equipments') or []),
                    },
                    'snapshot': {
                        'adventurer_id': adventurer_id,
                        'character_class': old_class,
                        'traits': [],
                        'skills': [],
                        'equipment': equipment,
                    },
                })
        return signals, {
            'logs_scanned': logs_scanned,
            'signals': len(signals),
            'scan_complete': True,
            'scan_truncated': False,
            'scan_truncated_reason': None,
            'scan_log_limit': None,
            'scan_signal_limit': None,
            'scan_page_size': page_size,
            'class_field_conflicts': class_field_conflicts,
        }

    @classmethod
    def _apply_catalog_constraints(
        cls,
        signals: list[dict],
    ) -> tuple[list[dict], list[dict], dict]:
        """确认可自动回收项；过滤无关卷轴，仅保留多凭证歧义供核查。"""
        item_names = {
            cls._name(item.get('name'))
            for signal in signals
            for item in (signal.get('snapshot', {}).get('equipment') or [])
            if isinstance(item, dict) and cls._name(item.get('name'))
        }
        if not item_names:
            return [], [], {
                'catalog_rejected': 0,
                'dungeon_rejected': 0,
                'class_rejected': 0,
                'ambiguous_rejected': 0,
            }
        catalogs = db_manager.query(AdventurerS1Item).filter(
            AdventurerS1Item.name.in_(sorted(item_names)),
        ).all()
        catalog_by_name = {row.name: row for row in catalogs}
        eligible: list[dict] = []
        review_candidates: list[dict] = []
        catalog_rejected = 0
        dungeon_rejected = 0
        # 保留历史汇总字段以兼容管理端；new_class 只作为“本轮转职已发生”的
        # 显式信号，不再与图鉴 target_class 做职业名相等判断。
        class_rejected = 0
        ambiguous_rejected = 0
        for signal in signals:
            equipment_by_name: dict[str, dict] = {}
            actual_dungeon = cls._name(signal.get('target_dungeon'))
            for item in signal.get('snapshot', {}).get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                item_name = cls._name(item.get('name'))
                catalog = catalog_by_name.get(item_name)
                condition = normalize_use_condition(catalog.use_condition) if catalog else None
                expected_dungeon = cls._name((condition or {}).get('expedition_dungeon'))
                target_class = cls._name((condition or {}).get('target_class'))
                if not is_class_transfer_invitation(condition):
                    catalog_rejected += 1
                    continue
                if not class_transfer_invitation_matches_dungeon(
                    condition,
                    actual_dungeon,
                ):
                    dungeon_rejected += 1
                    continue
                equipment_by_name.setdefault(item_name, {
                    **item,
                    'type': cls._name(catalog.type),
                    'rarity': cls._name(catalog.rarity) or 'common',
                    'description': (
                        cls._name(item.get('description'))
                        or cls._name(catalog.description)
                    ),
                    'use_condition': {
                        'expedition_dungeon': expected_dungeon,
                        'target_class': target_class,
                    },
                })
            equipment = list(equipment_by_name.values())
            if len(equipment) == 1:
                eligible.append({
                    **signal,
                    'snapshot': {**signal['snapshot'], 'equipment': equipment},
                })
            elif len(equipment) > 1:
                ambiguous_rejected += 1
                for item in equipment:
                    item_name = cls._name(item.get('name'))
                    review_candidates.append({
                        'signal': signal,
                        'item': item,
                        'reason_code': 'multiple_transfer_invitations',
                        'reason': (
                            '同一成员在该次转职结算中存在多种符合条件的邀请函，'
                            '无法确定应回收哪一种。'
                        ),
                    })
        return eligible, review_candidates, {
            'catalog_rejected': catalog_rejected,
            'dungeon_rejected': dungeon_rejected,
            'class_rejected': class_rejected,
            'ambiguous_rejected': ambiguous_rejected,
        }

    @staticmethod
    def _reserved_quantity(
        reservations: dict[str, dict[object, int]] | None,
        bucket: str,
        key: object,
    ) -> int:
        if reservations is None:
            return 0
        return int(reservations.setdefault(bucket, {}).get(key, 0) or 0)

    @staticmethod
    def _reserve_quantity(
        reservations: dict[str, dict[object, int]] | None,
        bucket: str,
        key: object,
    ) -> None:
        if reservations is None:
            return
        claims = reservations.setdefault(bucket, {})
        claims[key] = int(claims.get(key, 0) or 0) + 1

    @classmethod
    def _find_current_stack(
        cls,
        adventurer_id: int,
        item_name: str,
        reservations: dict[str, dict[object, int]] | None = None,
    ):
        for model, source in (
            (AdventurerS1Equipment, 'equipment'),
            (AdventurerS1WarehouseItem, 'warehouse'),
        ):
            rows = (
                db_manager.query(model)
                .filter(model.adventurer_id == adventurer_id, model.name == item_name)
                .order_by(model.id.asc())
                .all()
            )
            for row in rows:
                stack_key = (source, int(row.id))
                if item_quantity_of(row) > cls._reserved_quantity(
                    reservations,
                    'stacks',
                    stack_key,
                ):
                    return source, row
        return None

    @staticmethod
    def _currency_amount(adventurer_id: int, source_type: str, listing_id: int) -> float:
        rows = db_manager.query(AdventurerS1CurrencyLog).filter_by(
            adventurer_id=adventurer_id,
            currency_code='gold',
            source_type=source_type,
            related_id=str(listing_id),
            related_type='market_listing',
        ).all()
        if source_type == 'market_buy':
            return round(sum(abs(float(row.change_amount or 0)) for row in rows if float(row.change_amount or 0) < 0), 2)
        amount = sum(
            float(row.change_amount or 0)
            for row in rows
            if float(row.change_amount or 0) > 0
        )
        if source_type == 'market_sell':
            temporary_rows = db_manager.query(AdventurerS1CurrencyLog).filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.currency_code
                == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                AdventurerS1CurrencyLog.source_type.in_([
                    'low_consumption_accrual',
                    'low_consumption_split',
                ]),
                AdventurerS1CurrencyLog.related_id == str(listing_id),
                AdventurerS1CurrencyLog.related_type == 'market_listing',
                AdventurerS1CurrencyLog.change_amount > 0,
            ).all()
            amount += sum(float(row.change_amount or 0) for row in temporary_rows)
        return round(amount, 2)

    @staticmethod
    def _listing_total_buyer_paid(listing_id: int) -> float:
        rows = db_manager.query(AdventurerS1CurrencyLog.change_amount).filter_by(
            currency_code='gold',
            source_type='market_buy',
            related_id=str(listing_id),
            related_type='market_listing',
        ).all()
        return round(sum(abs(float(row[0] or 0)) for row in rows if float(row[0] or 0) < 0), 2)

    @classmethod
    def _missing_item_route(cls, item: dict, message_prefix: str, **details) -> dict:
        charge = cls._missing_item_charge(item.get('rarity'))
        return {
            'kind': 'missing_item_charge',
            'executable': charge > 0,
            'missing_item_charge_gold': charge,
            'seller_gold_clawback': 0.0,
            'buyer_refund_gold': 0.0,
            'message': (
                f'{message_prefix}，按图鉴稀有度向原转职者扣除 {charge:g} 金币。'
                if charge > 0 else f'{message_prefix}，但该稀有度未配置缺失扣款金额，本项跳过。'
            ),
            **details,
        }

    @classmethod
    def _plan_route(
        cls,
        signal: dict,
        item: dict,
        reservations: dict[str, dict[object, int]] | None = None,
    ) -> dict:
        adventurer_id = signal['adventurer_id']
        item_name = cls._name(item.get('name'))
        current = cls._find_current_stack(adventurer_id, item_name, reservations)
        if current:
            source, row = current
            cls._reserve_quantity(
                reservations,
                'stacks',
                (source, int(row.id)),
            )
            return {
                'kind': 'current_item',
                'executable': True,
                'source': source,
                'item_id': int(row.id),
                'missing_item_charge_gold': 0.0,
                'seller_gold_clawback': 0.0,
                'buyer_refund_gold': 0.0,
                'message': '转职凭证仍由原冒险者持有，直接回收 1 份，不扣金币。',
            }

        listings = (
            db_manager.query(AdventurerS1MarketListing)
            .filter(
                AdventurerS1MarketListing.seller_adventurer_id == adventurer_id,
                AdventurerS1MarketListing.item_name == item_name,
                AdventurerS1MarketListing.created_at >= signal['created_at'],
            )
            .order_by(AdventurerS1MarketListing.created_at.asc())
            .all()
        )
        missing_route = None
        for listing in listings:
            listing_id = int(listing.id)
            reserved_listing_quantity = cls._reserved_quantity(
                reservations,
                'listings',
                listing_id,
            )
            if listing.status == 'active':
                if item_quantity_of(listing) <= reserved_listing_quantity:
                    continue
                cls._reserve_quantity(
                    reservations,
                    'listings',
                    listing_id,
                )
                return {
                    'kind': 'active_listing',
                    'executable': True,
                    'listing_id': listing_id,
                    'missing_item_charge_gold': 0.0,
                    'seller_gold_clawback': 0.0,
                    'buyer_refund_gold': 0.0,
                    'message': '转职凭证仍在市场托管中，将从挂单回收 1 份，不扣金币。',
            }
            if listing.status != 'completed' or listing.listing_type != 'sell':
                continue
            buyer_id = int(listing.buyer_adventurer_id or 0)
            if buyer_id <= 0:
                if missing_route is None:
                    missing_route = cls._missing_item_route(
                        item,
                        '已成交但无法定位买家现物',
                        listing_id=listing_id,
                    )
                continue
            gain_log = (
                db_manager.query(AdventurerS1ItemLog)
                .filter_by(
                    adventurer_id=buyer_id,
                    item_name=item_name,
                    change_type='gain',
                    source_type='market',
                    related_id=str(listing.id),
                    related_type='market_listing',
                )
                .order_by(AdventurerS1ItemLog.created_at.desc())
                .first()
            )
            purchased_qty = (
                max(1, int(gain_log.quantity or 1))
                if gain_log else max(1, item_quantity_of(listing))
            )
            if purchased_qty <= reserved_listing_quantity:
                continue
            buyer_stack = cls._find_current_stack(
                buyer_id,
                item_name,
                reservations,
            )
            if not buyer_stack:
                if missing_route is None:
                    missing_route = cls._missing_item_route(
                        item,
                        '已成交且买家当前没有可回收的对应物品',
                        listing_id=listing_id,
                        buyer_adventurer_id=buyer_id,
                    )
                continue
            source, row = buyer_stack
            buyer_paid = cls._currency_amount(buyer_id, 'market_buy', listing.id)
            seller_income = cls._currency_amount(adventurer_id, 'market_sell', listing.id)
            total_buyer_paid = cls._listing_total_buyer_paid(listing.id)
            buyer_refund = round(buyer_paid / purchased_qty, 2)
            seller_clawback = round(
                seller_income * (buyer_refund / total_buyer_paid), 2,
            ) if total_buyer_paid > 0 else 0.0
            cls._reserve_quantity(
                reservations,
                'listings',
                listing_id,
            )
            cls._reserve_quantity(
                reservations,
                'stacks',
                (source, int(row.id)),
            )
            return {
                'kind': 'market_sold',
                'executable': True,
                'listing_id': listing_id,
                'buyer_adventurer_id': buyer_id,
                'buyer_source': source,
                'buyer_item_id': int(row.id),
                'missing_item_charge_gold': 0.0,
                'buyer_refund_gold': buyer_refund,
                'seller_gold_clawback': seller_clawback,
                'message': '已成交：回收买家 1 份，退还实付金币，并扣回卖家实际到账金币。',
            }
        return missing_route or cls._missing_item_route(
            item,
            '当前未找到可回收的对应物品',
        )

    @classmethod
    def _preview_item(
        cls,
        signal: dict,
        item: dict,
        reservations: dict[str, dict[object, int]],
        adventurers_by_id: dict[int, AdventurerS1],
    ) -> dict:
        adv = adventurers_by_id.get(int(signal['adventurer_id']))
        route = cls._plan_route(signal, item, reservations)
        item_name = cls._name(item.get('name'))
        item_key = hashlib.sha256(item_name.encode('utf-8')).hexdigest()[:16]
        return {
            'issue_key': f'class_scroll:{signal["log_id"]}:{signal["adventurer_id"]}:{item_key}',
            'issue_type': 'class_scroll_missing_consumption',
            'expedition_log_id': signal['log_id'],
            'adventurer_id': signal['adventurer_id'],
            'user_id': int(adv.owner_id) if adv and adv.owner_id else None,
            'adventurer_name': adv.name if adv else None,
            'old_class': signal['old_class'],
            'new_class': signal['new_class'],
            'target_dungeon': signal['target_dungeon'],
            'item_name': item_name,
            'item_type': cls._name(item.get('type')),
            'item_rarity': cls._name(item.get('rarity')) or 'common',
            'item_description': cls._name(item.get('description')),
            'quantity': 1,
            'route': route,
            'executable': bool(route.get('executable')),
        }

    @classmethod
    def _review_preview_item(
        cls,
        candidate: dict,
        adventurers_by_id: dict[int, AdventurerS1],
    ) -> dict:
        signal = candidate['signal']
        item = candidate['item']
        adv = adventurers_by_id.get(int(signal['adventurer_id']))
        item_name = cls._name(item.get('name'))
        reason_code = cls._name(candidate.get('reason_code')) or 'unconfirmed'
        item_key = hashlib.sha256(item_name.encode('utf-8')).hexdigest()[:16]
        condition = item.get('use_condition') or {}
        reason = cls._name(candidate.get('reason')) or '当前证据不足，不能自动回收。'
        return {
            'issue_key': (
                f'class_scroll_review:{signal["log_id"]}:'
                f'{signal["adventurer_id"]}:{item_key}:{reason_code}'
            ),
            'issue_type': 'class_scroll_missing_consumption_review',
            'expedition_log_id': signal['log_id'],
            'adventurer_id': signal['adventurer_id'],
            'user_id': int(adv.owner_id) if adv and adv.owner_id else None,
            'adventurer_name': adv.name if adv else None,
            'old_class': signal['old_class'],
            'new_class': signal['new_class'],
            'target_dungeon': signal['target_dungeon'],
            'catalog_target_dungeon': cls._name(condition.get('expedition_dungeon')),
            'catalog_target_class': cls._name(condition.get('target_class')),
            'item_name': item_name,
            'item_type': cls._name(item.get('type')),
            'item_rarity': cls._name(item.get('rarity')) or 'common',
            'item_description': cls._name(item.get('description')),
            'quantity': 1,
            'review_reason_code': reason_code,
            'review_reason': reason,
            'route': {
                'kind': 'manual_review',
                'executable': False,
                'message': reason,
            },
            'executable': False,
        }

    @classmethod
    def preview(cls, *, limit: int = 20, admin_user_id: int | None = None) -> tuple[dict, None]:
        limit = max(1, min(int(limit or 20), _MAX_PREVIEW_ITEMS))
        signals, scan = cls._collect_structured_signals()
        signals, review_candidates, condition_stats = cls._apply_catalog_constraints(signals)
        matched = [
            (signal, signal['snapshot']['equipment'][0])
            for signal in signals
            if len(signal.get('snapshot', {}).get('equipment') or []) == 1
        ][:limit]
        review_candidates_total = len(review_candidates)
        visible_review_candidates = review_candidates[:limit]
        adventurer_ids = {
            int(signal['adventurer_id'])
            for signal, _item in matched
        } | {
            int(candidate['signal']['adventurer_id'])
            for candidate in visible_review_candidates
        }
        adventurers_by_id = {
            int(row.id): row
            for row in (
                db_manager.query(AdventurerS1)
                .filter(AdventurerS1.id.in_(sorted(adventurer_ids)))
                .all()
                if adventurer_ids else []
            )
        }
        # 预览快照是后续执行合同；必须先按真实数量分配每个可回收来源，
        # 不能让多条候选冻结到同一份现物或同一个市场成交份额。
        reservations: dict[str, dict[object, int]] = {
            'stacks': {},
            'listings': {},
        }
        items = [
            cls._preview_item(signal, item, reservations, adventurers_by_id)
            for signal, item in matched
        ]
        review_items = [
            cls._review_preview_item(candidate, adventurers_by_id)
            for candidate in visible_review_candidates
        ]
        summary = {
            'matched': len(items),
            'review_required': review_candidates_total,
            'review_items_shown': len(review_items),
            'review_truncated': review_candidates_total > len(review_items),
            'executable': sum(1 for item in items if item['executable']),
            'unresolved': sum(1 for item in items if not item['executable']),
            'missing_item_candidates': sum(1 for item in items if item['route'].get('kind') == 'missing_item_charge'),
            'guard_calls': 0,
            'missing_item_charge_gold': round(sum(float(item['route'].get('missing_item_charge_gold') or 0) for item in items), 2),
            'seller_gold_clawback': round(sum(float(item['route'].get('seller_gold_clawback') or 0) for item in items), 2),
            'buyer_refund_gold': round(sum(float(item['route'].get('buyer_refund_gold') or 0) for item in items), 2),
            'ai_failures': 0,
            **condition_stats,
            **scan,
        }
        result = {
            'items': items,
            'review_items': review_items,
            'summary': summary,
        }
        if admin_user_id is not None and summary.get('scan_complete') is True:
            result['snapshot_token'] = S1AdminScanSnapshotService.store(
                _SNAPSHOT_OPERATION,
                admin_user_id,
                result,
                allow_signed_fallback=True,
            )
        return result, None

    @staticmethod
    def _lock_adventurers(ids: set[int]) -> None:
        if not ids:
            return
        (
            db_manager.query(AdventurerS1)
            .filter(AdventurerS1.id.in_(sorted(ids)))
            .order_by(AdventurerS1.id.asc())
            .with_for_update()
            .all()
        )

    @classmethod
    def _consume_locked_stack(cls, model, adventurer_id: int, item_id: int, item_name: str, description: str) -> None:
        row = (
            db_manager.query(model)
            .filter(model.id == item_id, model.adventurer_id == adventurer_id, model.name == item_name)
            .with_for_update()
            .first()
        )
        if not row or item_quantity_of(row) < 1:
            raise RuntimeError('目标物品已变化，请重新预览')
        _, err = consume_item_stack(
            row, 1,
            source_type='class_scroll_reclaim',
            description=description,
            hidden_from_player=False,
        )
        if err:
            raise RuntimeError(err)

    @classmethod
    def _execute_item(cls, item: dict, batch: AdventurerS1BugReclaimBatch, admin_user_id: int, now: datetime) -> dict:
        issue_key = cls._name(item.get('issue_key'))
        existing = db_manager.query(AdventurerS1BugReclaimItem.id).filter(
            AdventurerS1BugReclaimItem.issue_key == issue_key,
            AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
        ).first()
        if existing:
            return {**item, 'status': 'skipped', 'message': '已处理，跳过'}
        route = item.get('route') if isinstance(item.get('route'), dict) else {}
        if not item.get('executable') or not route.get('executable'):
            return {**item, 'status': 'skipped', 'message': route.get('message') or '当前不可安全执行'}

        seller_id = int(item['adventurer_id'])
        buyer_id = int(route.get('buyer_adventurer_id') or 0)
        kind = route.get('kind')
        removed = 0
        missing_item_charge = 0.0
        buyer_refund = 0.0
        seller_clawback = 0.0
        if kind == 'current_item':
            model = AdventurerS1Equipment if route.get('source') == 'equipment' else AdventurerS1WarehouseItem
            cls._consume_locked_stack(
                model, seller_id, int(route['item_id']), item['item_name'],
                f'管理员回收漏扣转职凭证「{item["item_name"]}」（探险 #{item["expedition_log_id"]}）',
            )
            removed = 1
        elif kind == 'active_listing':
            listing = (
                db_manager.query(AdventurerS1MarketListing)
                .filter_by(id=int(route['listing_id']), seller_adventurer_id=seller_id, status='active')
                .with_for_update()
                .first()
            )
            if not listing or listing.item_name != item['item_name']:
                raise RuntimeError('市场挂单已变化，请重新预览')
            qty = item_quantity_of(listing)
            if qty <= 1:
                listing.status = 'cancelled'
                listing.quantity = 1
            else:
                old_price = float(listing.price or 0)
                listing.quantity = qty - 1
                if listing.listing_type == 'sell':
                    listing.price = round(old_price * ((qty - 1) / qty), 2)
            listing.updated_at = now
            removed = 1
        elif kind == 'market_sold':
            model = AdventurerS1Equipment if route.get('buyer_source') == 'equipment' else AdventurerS1WarehouseItem
            cls._consume_locked_stack(
                model, buyer_id, int(route['buyer_item_id']), item['item_name'],
                f'管理员回收历史漏扣转职凭证「{item["item_name"]}」，并退还市场成交款',
            )
            removed = 1
            buyer_refund = round(float(route.get('buyer_refund_gold') or 0), 2)
            seller_clawback = round(float(route.get('seller_gold_clawback') or 0), 2)
            if buyer_refund > 0:
                _, err = S1CurrencyService.add(
                    buyer_id, 'gold', buyer_refund,
                    source_type='class_scroll_reclaim', related_id=batch.id,
                    related_type='bug_reclaim_batch',
                    description=f'历史漏扣转职凭证市场退款「{item["item_name"]}」',
                    auto_commit=False,
                )
                if err:
                    raise RuntimeError(err)
            if seller_clawback > 0:
                _, err = S1CurrencyService.deduct(
                    seller_id, 'gold', seller_clawback,
                    source_type='class_scroll_reclaim', related_id=batch.id,
                    related_type='bug_reclaim_batch',
                    description=f'历史漏扣转职凭证售出款扣回「{item["item_name"]}」',
                    auto_commit=False, allow_negative=True,
                )
                if err:
                    raise RuntimeError(err)
        elif kind == 'missing_item_charge':
            missing_item_charge = round(float(route.get('missing_item_charge_gold') or 0), 2)
            if missing_item_charge <= 0:
                raise RuntimeError('转职凭证缺失扣款金额无效，请重新预览')
            _, err = S1CurrencyService.deduct(
                seller_id, 'gold', missing_item_charge,
                source_type='class_scroll_reclaim', related_id=batch.id,
                related_type='bug_reclaim_batch',
                description=f'历史漏扣转职凭证无现物扣款「{item["item_name"]}」',
                auto_commit=False, allow_negative=True,
            )
            if err:
                raise RuntimeError(err)
        else:
            raise RuntimeError('不支持的回收路线，请重新预览')

        result = {
            'route_kind': kind,
            'removed_item_quantity': removed,
            'missing_item_charge_gold': missing_item_charge,
            'buyer_refund_gold': buyer_refund,
            'seller_gold_clawback': seller_clawback,
        }
        db_manager.add(AdventurerS1BugReclaimItem(
            batch=batch,
            status='executed',
            issue_key=issue_key,
            issue_type='class_scroll_missing_consumption',
            appeal_id=None,
            audit_log_id=None,
            adventurer_id=seller_id,
            user_id=item.get('user_id'),
            item_name=item['item_name'],
            item_type=item.get('item_type') or '',
            item_rarity=item.get('item_rarity') or 'common',
            illegal_quantity=1,
            reclaimed_item_quantity=removed,
            gold_deducted=round(seller_clawback + missing_item_charge, 2),
            before_json=item,
            after_json=result,
            route_json=[route],
            result_json=result,
            executed_by=admin_user_id,
            executed_at=now,
        ))
        return {**item, 'status': 'executed', 'result': result}

    @classmethod
    def execute(cls, *, admin_user_id: int, limit: int = 20, preview_token: str = '') -> tuple[dict | None, str | None]:
        if not preview_token:
            return None, '请先预览并使用本次预览快照执行'
        preview = S1AdminScanSnapshotService.load(
            _SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        preview_summary = preview.get('summary') or {}
        if preview_summary.get('scan_complete') is not True:
            return None, '预览扫描未完成，请重新完成全历史预览后再执行'
        items = list(preview.get('items') or [])
        now = _portable_utc_now()
        batch = AdventurerS1BugReclaimBatch(
            admin_user_id=admin_user_id,
            status='executed',
            criteria_json={'mode': 'class_scroll_missing_consumption', 'limit': limit},
            summary_json=preview.get('summary') or {},
            note='管理员执行历史漏扣转职凭证回收',
            executed_at=now,
        )
        results: list[dict] = []
        try:
            with db_manager.transaction():
                db_manager.add(batch)
                involved_ids = {
                    int(raw_id)
                    for item in items
                    for raw_id in (
                        item.get('adventurer_id'),
                        (item.get('route') or {}).get('buyer_adventurer_id'),
                    )
                    if raw_id
                }
                cls._lock_adventurers(involved_ids)
                for item in items:
                    results.append(cls._execute_item(item, batch, admin_user_id, now))
        except Exception as exc:
            return None, f'转职凭证回收失败：{exc}'
        active_listing_ids = [
            int((row.get('route') or {}).get('listing_id'))
            for row in results
            if row.get('status') == 'executed'
            and (row.get('route') or {}).get('kind') == 'active_listing'
            and (row.get('route') or {}).get('listing_id')
        ]
        if active_listing_ids:
            from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService

            for listing_id in active_listing_ids:
                S1MarketService._emit_market_changed('class_scroll_reclaimed', listing_id)
        S1AdminScanSnapshotService.delete(_SNAPSHOT_OPERATION, admin_user_id, preview_token)
        summary = {
            'count': len(results),
            'executed': sum(1 for row in results if row.get('status') == 'executed'),
            'skipped': sum(1 for row in results if row.get('status') == 'skipped'),
            'missing_item_charge_gold': round(sum(float((row.get('result') or {}).get('missing_item_charge_gold') or 0) for row in results), 2),
            'buyer_refund_gold': round(sum(float((row.get('result') or {}).get('buyer_refund_gold') or 0) for row in results), 2),
            'seller_gold_clawback': round(sum(float((row.get('result') or {}).get('seller_gold_clawback') or 0) for row in results), 2),
        }
        return {'batch': batch.to_dict(), 'items': results, 'summary': summary}, None
