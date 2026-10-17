"""S1 物品图鉴管理与未定制实例同步。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from contextlib import nullcontext
from datetime import datetime
import json
from types import SimpleNamespace
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func, literal, or_, select, union_all

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1MarketBuyOffer,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
    AdventurerS1SecretAuctionLot,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    apply_catalog_default_uses_left,
    catalog_default_uses_left,
    catalog_has_default_uses_left,
    is_hidden_boss_clue_item,
    normalize_use_condition,
)


class S1ItemCatalogService:
    """更新图鉴，并只迁移仍完整继承旧图鉴的当前资产。"""

    _CURRENT_TARGETS = (
        (
            AdventurerS1Equipment,
            'name',
            'type',
            'rarity',
            'custom_desc',
            'uses_left',
            None,
            'equipment',
        ),
        (
            AdventurerS1WarehouseItem,
            'name',
            'type',
            'rarity',
            'custom_desc',
            'uses_left',
            None,
            'warehouse',
        ),
        (
            AdventurerS1MarketListing,
            'item_name',
            'item_type',
            'rarity',
            'custom_desc',
            'uses_left',
            ('status', 'active'),
            'market_listings',
        ),
        (
            AdventurerS1MarketOffer,
            'offered_item_name',
            'offered_item_type',
            'offered_rarity',
            'offered_custom_desc',
            'offered_uses_left',
            ('status', 'pending'),
            'market_offers',
        ),
        (
            AdventurerS1MarketBuyOffer,
            'item_name',
            'item_type',
            'rarity',
            'custom_desc',
            'uses_left',
            ('status', 'pending'),
            'market_buy_offers',
        ),
        (
            AdventurerS1SecretAuctionLot,
            'item_name',
            'item_type',
            'rarity',
            'custom_desc',
            'uses_left',
            ('status', 'open'),
            'secret_auction_lots',
        ),
    )

    _MARKET_RARITY_GUARDS = _CURRENT_TARGETS[2:]
    _OWNER_FIELDS = {
        AdventurerS1Equipment: 'adventurer_id',
        AdventurerS1WarehouseItem: 'adventurer_id',
        AdventurerS1MarketListing: 'seller_adventurer_id',
        AdventurerS1MarketOffer: 'buyer_adventurer_id',
        AdventurerS1MarketBuyOffer: 'seller_adventurer_id',
    }
    # 挂单/拍卖是托管实例，需要持久化展示描述。两类 offer 仍引用仓库原实例，
    # custom_desc 必须与仓库保持严格一致，读取时再按新名称回退图鉴描述。
    _PERSISTED_DESCRIPTION_TARGETS = frozenset({
        AdventurerS1MarketListing,
        AdventurerS1SecretAuctionLot,
    })

    @staticmethod
    def _snapshot(item: AdventurerS1Item) -> dict:
        return {
            'name': item.name,
            'type': item.type,
            'description': item.description or '',
            'rarity': item.rarity,
        }

    @staticmethod
    def _matching_query(target, old: dict):
        (
            model,
            name_field,
            type_field,
            rarity_field,
            desc_field,
            _uses_field,
            status_filter,
            _count_key,
        ) = target
        name_column = getattr(model, name_field)
        type_column = getattr(model, type_field)
        rarity_column = getattr(model, rarity_field)
        desc_column = getattr(model, desc_field)
        query = db_manager.query(model)
        if status_filter:
            status_field, status_value = status_filter
            query = query.filter(getattr(model, status_field) == status_value)
        return query.filter(
            name_column == old['name'],
            type_column == old['type'],
            rarity_column == old['rarity'],
            or_(
                desc_column.is_(None),
                desc_column == '',
                desc_column == old['description'],
            ),
        )

    @classmethod
    def _target_query(
        cls,
        target,
        *,
        old: Optional[dict] = None,
        target_ids: Optional[dict[str, list[int]]] = None,
    ):
        model = target[0]
        if old is not None:
            return cls._matching_query(target, old)
        ids = [
            int(row_id)
            for row_id in (target_ids or {}).get(target[-1], [])
            if int(row_id) > 0
        ]
        if not ids:
            return None
        return db_manager.query(model).filter(model.id.in_(sorted(set(ids))))

    @classmethod
    def _current_reference_counts(cls, item_name: str) -> dict[str, int]:
        """Count all live references in one statement snapshot to avoid move-between-table gaps."""
        statements = []
        counts = {target[-1]: 0 for target in cls._CURRENT_TARGETS}
        for target in cls._CURRENT_TARGETS:
            model, name_field, *_rest, status_filter, count_key = target
            statement = select(
                literal(count_key).label('target'),
                model.id.label('row_id'),
            ).where(getattr(model, name_field) == item_name)
            if status_filter:
                status_field, status_value = status_filter
                statement = statement.where(
                    getattr(model, status_field) == status_value,
                )
            statements.append(statement)
        references = union_all(*statements).subquery()
        for target, count in db.session.execute(
            select(references.c.target, func.count())
            .group_by(references.c.target),
        ).all():
            counts[str(target)] = int(count or 0)
        return counts

    @classmethod
    def _lock_current_asset_rows(
        cls,
        *,
        old: Optional[dict] = None,
        target_ids: Optional[dict[str, list[int]]] = None,
        max_rows: Optional[int] = None,
    ) -> dict[str, list]:
        """Lock one stable asset set before any catalog-driven bulk update.

        Secret-auction settlement locks its lot before granting into a warehouse, so open
        lots come first. All adventurer-owned targets then follow the shared order:
        AdventurerS1 -> warehouse aggregate -> child rows by model/id.
        """
        if (old is None) == (target_ids is None):
            raise ValueError('old 与 target_ids 必须且只能提供一个')

        if max_rows is not None:
            max_rows = max(1, int(max_rows))
            projected_count = 0
            for target in cls._CURRENT_TARGETS:
                query = cls._target_query(target, old=old, target_ids=target_ids)
                if query is None:
                    continue
                remaining = max_rows - projected_count
                target_count = int(query.limit(remaining + 1).count())
                if target_count > remaining:
                    raise RuntimeError(
                        f'待修复资产超过单次安全上限 {max_rows} 条，请使用分批修复工具'
                    )
                projected_count += target_count

        rows_by_target = {target[-1]: [] for target in cls._CURRENT_TARGETS}
        auction_target = next(
            target
            for target in cls._CURRENT_TARGETS
            if target[0] is AdventurerS1SecretAuctionLot
        )
        auction_query = cls._target_query(
            auction_target,
            old=old,
            target_ids=target_ids,
        )
        if auction_query is not None:
            auction_locked_query = (
                auction_query
                .order_by(AdventurerS1SecretAuctionLot.id.asc())
                .populate_existing()
                .with_for_update()
            )
            if max_rows is not None:
                auction_locked_query = auction_locked_query.limit(max_rows + 1)
            rows_by_target[auction_target[-1]] = auction_locked_query.all()
            if (
                max_rows is not None
                and len(rows_by_target[auction_target[-1]]) > max_rows
            ):
                raise RuntimeError(
                    f'待修复资产超过单次安全上限 {max_rows} 条，请使用分批修复工具'
                )

        owner_ids = set()
        for target in cls._CURRENT_TARGETS:
            model = target[0]
            owner_field = cls._OWNER_FIELDS.get(model)
            if owner_field is None:
                continue
            query = cls._target_query(target, old=old, target_ids=target_ids)
            if query is None:
                continue
            owner_column = getattr(model, owner_field)
            owner_ids.update(
                int(row[0])
                for row in query.with_entities(owner_column).distinct().all()
                if row[0] is not None and int(row[0]) > 0
            )

        sorted_owner_ids = sorted(owner_ids)
        if sorted_owner_ids:
            (
                db_manager.query(AdventurerS1)
                .filter(AdventurerS1.id.in_(sorted_owner_ids))
                .order_by(AdventurerS1.id.asc())
                .populate_existing()
                .with_for_update()
                .all()
            )
            from plugins.adventurer_s1.services.s1_item_instance_service import (
                lock_warehouse_aggregates,
            )

            lock_warehouse_aggregates(sorted_owner_ids)

        for target in cls._CURRENT_TARGETS:
            model = target[0]
            if model is AdventurerS1SecretAuctionLot:
                continue
            query = cls._target_query(target, old=old, target_ids=target_ids)
            if query is None:
                continue
            locked_query = (
                query
                .order_by(model.id.asc())
                .populate_existing()
                .with_for_update()
            )
            if max_rows is not None:
                remaining = max_rows - sum(len(value) for value in rows_by_target.values())
                locked_query = locked_query.limit(remaining + 1)
            rows = locked_query.all()
            if max_rows is not None and (
                sum(len(value) for value in rows_by_target.values()) + len(rows) > max_rows
            ):
                raise RuntimeError(
                    f'待修复资产超过单次安全上限 {max_rows} 条，请使用分批修复工具'
                )
            owner_field = cls._OWNER_FIELDS[model]
            unexpected_owner_ids = {
                int(getattr(row, owner_field))
                for row in rows
                if getattr(row, owner_field) is not None
                and int(getattr(row, owner_field)) not in owner_ids
            }
            if unexpected_owner_ids:
                raise RuntimeError('待同步资产归属在加锁时发生变化，请重试')
            rows_by_target[target[-1]] = rows
        return rows_by_target

    @classmethod
    def _market_rarity_conflicts(
        cls,
        old: dict,
        locked_rows: Optional[dict[str, list]] = None,
    ) -> dict:
        return {
            target[-1]: (
                len(locked_rows.get(target[-1], []))
                if locked_rows is not None
                else int(cls._matching_query(target, old).count())
            )
            for target in cls._MARKET_RARITY_GUARDS
        }

    @classmethod
    def _sync_current_assets(
        cls,
        old: dict,
        new: dict,
        locked_rows: Optional[dict[str, list]] = None,
    ) -> dict:
        now = _portable_utc_now()
        counts = {
            'equipment': 0,
            'warehouse': 0,
            'market_listings': 0,
            'market_offers': 0,
            'market_buy_offers': 0,
            'secret_auction_lots': 0,
        }
        for target in cls._CURRENT_TARGETS:
            (
                model,
                name_field,
                type_field,
                rarity_field,
                desc_field,
                _uses_field,
                _status_filter,
                count_key,
            ) = target
            values = {
                getattr(model, name_field): new['name'],
                getattr(model, type_field): new['type'],
                getattr(model, rarity_field): new['rarity'],
                getattr(model, desc_field): (
                    new['description']
                    if model in cls._PERSISTED_DESCRIPTION_TARGETS
                    else None
                ),
                model.updated_at: now,
            }
            query = cls._matching_query(target, old)
            if locked_rows is not None:
                target_ids = [row.id for row in locked_rows.get(count_key, [])]
                if not target_ids:
                    continue
                query = db_manager.query(model).filter(model.id.in_(target_ids))
            counts[count_key] = int(query.update(values, synchronize_session=False))
        counts['market_total'] = (
            counts['market_listings']
            + counts['market_offers']
            + counts['market_buy_offers']
            + counts['secret_auction_lots']
        )
        counts['total'] = (
            counts['equipment']
            + counts['warehouse']
            + counts['market_total']
        )
        return counts

    @staticmethod
    def _empty_counts() -> dict:
        return {
            'equipment': 0,
            'warehouse': 0,
            'market_listings': 0,
            'market_offers': 0,
            'market_buy_offers': 0,
            'secret_auction_lots': 0,
            'market_total': 0,
            'total': 0,
        }

    @staticmethod
    def _parse_uses_left(body: dict) -> tuple[Optional[int], bool]:
        if 'uses_left' not in body:
            return None, False
        raw = body.get('uses_left')
        if raw is None or raw == '':
            return None, True
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError('使用次数无效')
        if value < 0:
            raise ValueError('使用次数不能为负')
        return value, True

    @classmethod
    def _sync_default_uses(
        cls,
        old: dict,
        old_use_condition: Optional[dict],
        new_use_condition: Optional[dict],
        locked_rows: Optional[dict[str, list]] = None,
    ) -> tuple[dict, bool]:
        counts = cls._empty_counts()
        if not catalog_has_default_uses_left(old_use_condition):
            # 旧图鉴没有默认次数时，无法证明存量 NULL 代表“从未使用”，不做补发。
            return counts, False

        old_default = catalog_default_uses_left(old_use_condition)
        new_has_default = catalog_has_default_uses_left(new_use_condition)
        new_default = (
            catalog_default_uses_left(new_use_condition)
            if new_has_default
            else None
        )
        if new_has_default and new_default == old_default:
            return counts, False

        now = _portable_utc_now()
        for target in cls._CURRENT_TARGETS:
            model = target[0]
            uses_field = target[5]
            count_key = target[-1]
            uses_column = getattr(model, uses_field)
            query = cls._matching_query(target, old).filter(
                uses_column == old_default,
            )
            if locked_rows is not None:
                target_ids = [
                    row.id
                    for row in locked_rows.get(count_key, [])
                    if getattr(row, uses_field) == old_default
                ]
                if not target_ids:
                    continue
                query = db_manager.query(model).filter(model.id.in_(target_ids))
            counts[count_key] = int(query.update(
                {
                    uses_column: new_default,
                    model.updated_at: now,
                },
                synchronize_session=False,
            ))
        counts['market_total'] = (
            counts['market_listings']
            + counts['market_offers']
            + counts['market_buy_offers']
            + counts['secret_auction_lots']
        )
        counts['total'] = (
            counts['equipment']
            + counts['warehouse']
            + counts['market_total']
        )
        return counts, True

    @staticmethod
    def _emit_catalog_changed(item_id: int) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio

            socketio.emit(
                'adventurer_changed',
                {'action': 'catalog_item_changed', 'catalog_item_id': item_id},
                namespace='/',
            )
            socketio.emit(
                'adventurer_s1_market_changed',
                {'action': 'catalog_item_changed', 'catalog_item_id': item_id},
                namespace='/',
            )
            socketio.emit(
                'adventurer_s1_secret_auction_changed',
                {'action': 'catalog_item_changed', 'catalog_item_id': item_id},
                namespace='/',
            )
        except Exception:
            current_app.logger.warning(
                'S1 图鉴更新后的实时通知发送失败 item_id=%s',
                item_id,
                exc_info=True,
            )

    @classmethod
    def refresh_after_catalog_change(cls, item_id: int) -> None:
        """提交后统一失效图鉴消费者缓存并广播刷新。"""
        try:
            from plugins.adventurer_s1.services.adventurer_s1_home_service import (
                invalidate_s1_init_cache,
            )
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                invalidate_adventurer_detail_cache,
            )

            invalidate_adventurer_detail_cache()
            invalidate_s1_init_cache()
        except Exception:
            current_app.logger.warning(
                'S1 图鉴更新后的缓存失效失败 item_id=%s',
                item_id,
                exc_info=True,
            )
        cls._emit_catalog_changed(item_id)

    @classmethod
    def update_catalog_item(
        cls,
        item_id: int,
        body: dict,
        *,
        auto_commit: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """更新图鉴；``auto_commit=False`` 时由外层业务事务统一提交或回滚。"""
        content_changed = False
        result = None
        try:
            transaction_context = db_manager.transaction() if auto_commit else nullcontext()
            with transaction_context:
                item_projection = (
                    db_manager.query(AdventurerS1Item.id, AdventurerS1Item.name)
                    .filter(AdventurerS1Item.id == item_id)
                    .first()
                )
                if not item_projection:
                    return None, '物品不存在'
                requested_name = (
                    (body.get('name') or '').strip()
                    if 'name' in body
                    else item_projection.name
                )
                if not requested_name:
                    return None, '物品名称不能为空'
                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
                    lock_catalog_names,
                )

                lock_catalog_names(
                    [item_projection.name, requested_name],
                    shared=False,
                    wait=True,
                )
                item = (
                    db_manager.query(AdventurerS1Item)
                    .filter(AdventurerS1Item.id == item_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not item:
                    return None, '物品不存在'
                if item.name != item_projection.name:
                    raise RuntimeError('图鉴名称在加锁前已变化，请重试')

                try:
                    uses_left, uses_left_provided = cls._parse_uses_left(body)
                except ValueError as exc:
                    return None, str(exc)

                if is_hidden_boss_clue_item(item.use_condition):
                    blocked = {
                        'name', 'description', 'type', 'rarity', 'world_unique',
                        'is_starter', 'use_condition', 'uses_left',
                    } & body.keys()
                    if blocked:
                        return None, (
                            '追查线索等系统锁定物品不可在此修改，'
                            '请走线索追查处图鉴同步'
                        )

                old = cls._snapshot(item)
                new_name = requested_name
                if 'name' in body and new_name != old['name']:
                    duplicate = (
                        db_manager.query(AdventurerS1Item.id)
                        .filter(
                            AdventurerS1Item.name == new_name,
                            AdventurerS1Item.id != item.id,
                        )
                        .first()
                    )
                    if duplicate:
                        return None, f'物品「{new_name}」已存在'
                    target_reference_counts = cls._current_reference_counts(new_name)
                    target_reference_total = sum(target_reference_counts.values())
                    if target_reference_total:
                        return None, (
                            f'无法改名为「{new_name}」：该名称已有 '
                            f'{target_reference_total} 条当前资产或交易引用'
                        )

                new_type = (
                    (body.get('type') or '').strip()
                    if 'type' in body
                    else old['type']
                )
                new_description = (
                    (body.get('description') or '').strip()
                    if 'description' in body
                    else old['description']
                )
                new_rarity = (
                    (body.get('rarity') or 'common').strip()
                    if 'rarity' in body
                    else old['rarity']
                )
                new = {
                    'name': new_name,
                    'type': new_type,
                    'description': new_description,
                    'rarity': new_rarity,
                }
                content_changed = new != old

                new_use_condition = (
                    normalize_use_condition(body.get('use_condition'))
                    if 'use_condition' in body
                    else item.use_condition
                )
                # 普通图鉴编辑不能意外清除内部资格回执；撤销须走带前值重检的还原入口。
                from plugins.adventurer_s1.services.s1_item_authority_contract import ITEM_AUTHORITY_REVIEW_KEY
                authority_review = (item.use_condition or {}).get(ITEM_AUTHORITY_REVIEW_KEY)
                if authority_review is not None:
                    new_use_condition = dict(new_use_condition or {})
                    new_use_condition[ITEM_AUTHORITY_REVIEW_KEY] = authority_review
                condition_holder = SimpleNamespace(use_condition=new_use_condition)
                uses_err = apply_catalog_default_uses_left(
                    condition_holder,
                    uses_left,
                    provided=uses_left_provided,
                )
                if uses_err:
                    return None, uses_err
                new_use_condition = condition_holder.use_condition
                use_condition_changed = new_use_condition != item.use_condition
                new_world_unique = (
                    bool(body.get('world_unique'))
                    if 'world_unique' in body
                    else item.world_unique
                )
                new_is_starter = (
                    bool(body.get('is_starter'))
                    if 'is_starter' in body
                    else item.is_starter
                )
                catalog_changed = (
                    content_changed
                    or use_condition_changed
                    or new_world_unique != item.world_unique
                    or new_is_starter != item.is_starter
                )
                from plugins.adventurer_s1.s1_item_types import (
                    companion_allows_duplicate_name_instances,
                )
                from plugins.adventurer_s1.services.s1_item_world_unique_service import (
                    world_unique_promotion_error,
                )

                old_effective_world_unique = bool(item.world_unique)
                new_effective_world_unique = bool(new_world_unique)
                uniqueness_became_stricter = bool(
                    new_effective_world_unique
                    and companion_allows_duplicate_name_instances(old['type'])
                    and not companion_allows_duplicate_name_instances(new_type)
                )
                if new_effective_world_unique and (
                    not old_effective_world_unique or uniqueness_became_stricter
                ):
                    promotion_error = world_unique_promotion_error(
                        old['name'],
                        item_type=new_type,
                    )
                    if promotion_error:
                        return None, promotion_error

                old_has_default = catalog_has_default_uses_left(item.use_condition)
                new_has_default = catalog_has_default_uses_left(new_use_condition)
                default_uses_will_change = bool(
                    old_has_default
                    and (
                        not new_has_default
                        or catalog_default_uses_left(item.use_condition)
                        != catalog_default_uses_left(new_use_condition)
                    )
                )
                locked_rows = (
                    cls._lock_current_asset_rows(old=old)
                    if content_changed or default_uses_will_change
                    else None
                )

                if new_rarity != old['rarity']:
                    market_conflicts = cls._market_rarity_conflicts(
                        old,
                        locked_rows=locked_rows,
                    )
                    conflict_total = sum(market_conflicts.values())
                    if conflict_total:
                        return None, (
                            '稀有度修改会改变现有市场的手续费、售价或求购档位，'
                            f'已拒绝保存；请先结束相关进行中交易（共 {conflict_total} 条）'
                        )

                if new_name != old['name']:
                    from plugins.adventurer_s1.services.s1_lucky_wheel_service import (
                        S1LuckyWheelService,
                    )

                    wheel_err = S1LuckyWheelService.sync_catalog_rename(
                        old['name'],
                        new_name,
                    )
                    if wheel_err:
                        return None, wheel_err

                default_uses_counts, default_uses_changed = cls._sync_default_uses(
                    old,
                    item.use_condition,
                    new_use_condition,
                    locked_rows=locked_rows,
                )
                sync_counts = cls._empty_counts()
                if content_changed:
                    sync_counts = cls._sync_current_assets(
                        old,
                        new,
                        locked_rows=locked_rows,
                    )

                item.name = new_name
                item.type = new_type
                item.description = new_description
                item.rarity = new_rarity
                item.world_unique = new_world_unique
                item.is_starter = new_is_starter
                item.use_condition = new_use_condition

                result = item.to_admin_dict()
                result['sync'] = {
                    **sync_counts,
                    'catalog_changed': catalog_changed,
                    'content_changed': content_changed,
                    'use_condition_changed': use_condition_changed,
                    'default_uses_changed': default_uses_changed,
                    'default_uses': default_uses_counts,
                    'matched_old_catalog_only': True,
                    'historical_market_rows_updated': 0,
                }
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 图鉴更新与实例同步失败 item_id=%s', item_id)
            return None, f'更新失败：{exc}'

        catalog_state_changed = bool(
            result
            and (
                result['sync'].get('catalog_changed')
                or result['sync'].get('default_uses_changed')
            )
        )
        if auto_commit and catalog_state_changed and result:
            cls.refresh_after_catalog_change(item_id)

        return result, None

    @classmethod
    def delete_catalog_item(
        cls,
        item_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        projection = (
            db_manager.query(AdventurerS1Item.id, AdventurerS1Item.name)
            .filter(AdventurerS1Item.id == int(item_id))
            .first()
        )
        if not projection:
            return None, '物品不存在'

        try:
            with db_manager.transaction():
                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
                    lock_catalog_names,
                )

                lock_catalog_names([projection.name], shared=False, wait=True)
                item = (
                    db_manager.query(AdventurerS1Item)
                    .filter(AdventurerS1Item.id == int(item_id))
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not item:
                    return None, '物品不存在'
                if item.name != projection.name:
                    raise RuntimeError('图鉴名称在加锁前已变化，请重试')
                if is_hidden_boss_clue_item(item.use_condition):
                    return None, '追查线索图鉴由线索追查处维护，不可在此删除'

                reference_counts = cls._current_reference_counts(item.name)
                reference_total = sum(reference_counts.values())
                if reference_total:
                    return None, (
                        f'无法删除「{item.name}」：仍有 {reference_total} 条当前资产或交易引用'
                    )

                from plugins.adventurer_s1.services.s1_lucky_wheel_service import (
                    S1LuckyWheelService,
                )

                if S1LuckyWheelService.catalog_name_is_referenced(item.name):
                    return None, f'无法删除「{item.name}」：幸运转盘奖池仍在引用'

                backup = (
                    db_manager.query(AdventurerS1Settings)
                    .filter_by(key=f'catalog_history_sync_backup:{item.id}')
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if backup and backup.value:
                    try:
                        payload = json.loads(backup.value)
                        runs = payload.get('runs') if isinstance(payload, dict) else None
                    except (TypeError, json.JSONDecodeError):
                        runs = None
                    if not isinstance(runs, list):
                        return None, '该图鉴的历史同步备份无效，不能删除'
                    if any(
                        isinstance(run, dict)
                        and str(run.get('status') or 'active') in {
                            'active',
                            'partially_restored',
                        }
                        for run in runs
                    ):
                        return None, '该图鉴仍有未完成还原的历史同步批次，不能删除'

                ok, delete_err = db_manager.delete_instance(item)
                if not ok:
                    raise RuntimeError(delete_err or '删除图鉴失败')
                result = {'message': f'已删除「{item.name}」', 'id': int(item.id)}
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 图鉴删除失败 item_id=%s', item_id)
            return None, f'删除失败：{exc}'

        cls.refresh_after_catalog_change(int(item_id))
        return result, None
