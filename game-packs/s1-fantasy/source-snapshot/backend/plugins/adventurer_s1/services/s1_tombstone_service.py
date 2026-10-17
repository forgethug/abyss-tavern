"""英灵碑林：阵亡冒险者墓碑登记、查询与留言。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload

from db_repository import db_manager
from db_repository.decorators import with_transaction
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1Currency,
    AdventurerS1ExpeditionLog,
    AdventurerS1Tombstone,
    AdventurerS1TombstoneMessage,
    AdventurerS1TombstoneTribute,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

_HANDLER_KEY = 'fallen_memorial'
_FLOWER_SHOP_HANDLER_KEY = 'flower_shop'
_DEFAULT_EPITAPH = '在一次探险中倒下，再未能归队。'
_RECENT_MESSAGE_LIMIT = 5


class S1TombstoneService:
    """阵亡冒险者墓碑业务逻辑。"""

    @staticmethod
    def _normalize_epitaph(text: str | None) -> str:
        value = (text or '').strip()
        return value or _DEFAULT_EPITAPH

    @staticmethod
    def record_death(
        adv: AdventurerS1,
        death_reason: str | None = None,
        *,
        expedition_log_id: int | None = None,
    ) -> None:
        """冒险者阵亡时登记或更新墓碑。须在事务内调用。"""
        epitaph = S1TombstoneService._normalize_epitaph(death_reason)
        now = _portable_utc_now()
        existing = AdventurerS1Tombstone.query.filter_by(adventurer_id=adv.id).first()
        if existing:
            existing.name = adv.name
            existing.character_class = adv.character_class
            existing.level = int(adv.level or 1)
            existing.owner_id = adv.owner_id
            existing.epitaph = epitaph
            existing.expedition_log_id = expedition_log_id
            existing.died_at = now
            existing.updated_at = now
            return

        db_manager.add(AdventurerS1Tombstone(
            adventurer_id=adv.id,
            owner_id=adv.owner_id,
            name=adv.name,
            character_class=adv.character_class,
            level=int(adv.level or 1),
            epitaph=epitaph,
            expedition_log_id=expedition_log_id,
            died_at=now,
        ))

    @staticmethod
    def _find_death_reason_from_logs(adventurer_id: int) -> tuple[str | None, int | None]:
        """从探险日志 member_updates 回溯最近一次结构化死亡原因。"""
        logs = (
            AdventurerS1ExpeditionLog.query
            .filter(AdventurerS1ExpeditionLog.status.in_(('completed', 'in_progress')))
            .order_by(AdventurerS1ExpeditionLog.id.desc())
            .limit(500)
            .all()
        )
        for log in logs:
            full_log = log.full_log if isinstance(log.full_log, dict) else {}
            for raw in full_log.get('member_updates') or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    member_id = int(raw.get('id'))
                except (TypeError, ValueError):
                    continue
                if member_id != adventurer_id:
                    continue
                remaining_hp = raw.get('remaining_hp')
                is_dead = raw.get('is_dead') is True
                if not is_dead and not (remaining_hp is not None and int(remaining_hp) <= 0):
                    continue
                reason = raw.get('death_reason')
                if isinstance(reason, str) and reason.strip():
                    return reason.strip(), log.id
                summary = (log.result_summary or '').strip()
                if summary:
                    return summary[:500], log.id
        return None, None

    @staticmethod
    def backfill_missing_tombstones(limit: int = 500) -> int:
        """为已阵亡但尚无墓碑的冒险者补登记（启动 seed 时调用）。"""
        created = 0
        dead_rows = (
            AdventurerS1.query
            .filter(AdventurerS1.status == 'dead')
            .order_by(AdventurerS1.id.asc())
            .limit(max(1, limit))
            .all()
        )
        if not dead_rows:
            return 0

        existing_ids = {
            row.adventurer_id
            for row in AdventurerS1Tombstone.query.filter(
                AdventurerS1Tombstone.adventurer_id.in_([a.id for a in dead_rows])
            ).all()
        }
        for adv in dead_rows:
            if adv.id in existing_ids:
                continue
            reason, log_id = S1TombstoneService._find_death_reason_from_logs(adv.id)
            S1TombstoneService.record_death(adv, reason, expedition_log_id=log_id)
            created += 1
        if created:
            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or 'backfill tombstones commit failed')
        return created

    @staticmethod
    def _resolve_memorial_instance(building_instance_id: int) -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst or inst.disabled:
            return None, '建筑不存在或已停用'
        if inst.status != 'built':
            return None, '英灵碑林尚未建成'
        tpl = inst.template
        if not tpl or tpl.handler_key != _HANDLER_KEY:
            return None, '该建筑不是英灵碑林'
        return inst, None

    @staticmethod
    def _resolve_tombstone(tombstone_id: int) -> Tuple[Optional[AdventurerS1Tombstone], Optional[str]]:
        stone = AdventurerS1Tombstone.query.session.get(AdventurerS1Tombstone, tombstone_id)
        if not stone:
            return None, '墓碑不存在'
        return stone, None

    @staticmethod
    def _resolve_flower_shop_instance() -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        inst = (
            AdventurerS1BuildingInstance.query
            .join(
                AdventurerS1BuildingTemplate,
                AdventurerS1BuildingTemplate.id == AdventurerS1BuildingInstance.template_id,
            )
            .filter(
                AdventurerS1BuildingTemplate.handler_key == _FLOWER_SHOP_HANDLER_KEY,
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.disabled.isnot(True),
                AdventurerS1BuildingInstance.status == 'built',
            )
            .order_by(AdventurerS1BuildingInstance.id.asc())
            .first()
        )
        if not inst:
            return None, '鲜花店尚未建成'
        return inst, None

    @staticmethod
    def _active_tribute_filter(now: datetime | None = None):
        current = now or _portable_utc_now()
        lifetime_hours = max(1, int(current_app.config.get(
            'ADVENTURER_S1_TOMBSTONE_FLOWER_LIFETIME_HOURS', 24,
        )))
        legacy_cutoff = current - timedelta(hours=lifetime_hours)
        return or_(
            AdventurerS1TombstoneTribute.expires_at > current,
            and_(
                AdventurerS1TombstoneTribute.expires_at.is_(None),
                AdventurerS1TombstoneTribute.created_at > legacy_cutoff,
            ),
        )

    @staticmethod
    def _attach_message_meta(rows: list[AdventurerS1Tombstone]) -> list[dict]:
        if not rows:
            return []
        stone_ids = [row.id for row in rows]
        count_rows = (
            AdventurerS1TombstoneMessage.query
            .with_entities(
                AdventurerS1TombstoneMessage.tombstone_id,
                func.count(AdventurerS1TombstoneMessage.id),
            )
            .filter(AdventurerS1TombstoneMessage.tombstone_id.in_(stone_ids))
            .group_by(AdventurerS1TombstoneMessage.tombstone_id)
            .all()
        )
        count_map = {tid: int(cnt) for tid, cnt in count_rows}

        msg_rows = (
            AdventurerS1TombstoneMessage.query
            .options(joinedload(AdventurerS1TombstoneMessage.user))
            .filter(AdventurerS1TombstoneMessage.tombstone_id.in_(stone_ids))
            .order_by(
                AdventurerS1TombstoneMessage.tombstone_id.asc(),
                AdventurerS1TombstoneMessage.created_at.desc(),
                AdventurerS1TombstoneMessage.id.desc(),
            )
            .all()
        )
        recent_map: dict[int, list[dict]] = defaultdict(list)
        for msg in msg_rows:
            bucket = recent_map[msg.tombstone_id]
            if len(bucket) >= _RECENT_MESSAGE_LIMIT:
                continue
            bucket.append(msg.to_dict())

        # 献花汇总：按墓碑分组统计总数 + 按品种汇总
        active_tribute_filter = S1TombstoneService._active_tribute_filter()
        tribute_count_rows = (
            AdventurerS1TombstoneTribute.query
            .with_entities(
                AdventurerS1TombstoneTribute.tombstone_id,
                func.sum(AdventurerS1TombstoneTribute.quantity),
            )
            .filter(AdventurerS1TombstoneTribute.tombstone_id.in_(stone_ids))
            .filter(active_tribute_filter)
            .group_by(AdventurerS1TombstoneTribute.tombstone_id)
            .all()
        )
        tribute_count_map = {tid: int(total or 0) for tid, total in tribute_count_rows}

        tribute_summary_rows = (
            AdventurerS1TombstoneTribute.query
            .with_entities(
                AdventurerS1TombstoneTribute.tombstone_id,
                AdventurerS1TombstoneTribute.flower_code,
                AdventurerS1TombstoneTribute.flower_name,
                func.sum(AdventurerS1TombstoneTribute.quantity),
            )
            .filter(AdventurerS1TombstoneTribute.tombstone_id.in_(stone_ids))
            .filter(active_tribute_filter)
            .group_by(
                AdventurerS1TombstoneTribute.tombstone_id,
                AdventurerS1TombstoneTribute.flower_code,
                AdventurerS1TombstoneTribute.flower_name,
            )
            .all()
        )
        tribute_summary_map: dict[int, list[dict]] = defaultdict(list)
        for tid, fcode, fname, total in tribute_summary_rows:
            tribute_summary_map[tid].append({
                'code': fcode,
                'name': fname,
                'emoji': AdventurerS1TombstoneTribute._resolve_flower_emoji(fcode),
                'total_quantity': int(total or 0),
            })

        return [
            row.to_dict(
                message_count=count_map.get(row.id, 0),
                recent_messages=list(reversed(recent_map.get(row.id, []))),
                tribute_count=tribute_count_map.get(row.id, 0),
                tribute_summary=tribute_summary_map.get(row.id, []),
            )
            for row in rows
        ]

    @staticmethod
    def list_tombstones(
        building_instance_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
        owner_id: int | None = None,
        mine_only: bool = False,
        search: str | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        _, err = S1TombstoneService._resolve_memorial_instance(building_instance_id)
        if err:
            return None, err

        page = max(1, int(page or 1))
        page_size = min(50, max(1, int(page_size or 20)))
        query = AdventurerS1Tombstone.query
        if mine_only and owner_id is not None:
            query = query.filter(AdventurerS1Tombstone.owner_id == owner_id)
        keyword = (search or '').strip()
        if keyword:
            query = query.filter(AdventurerS1Tombstone.name.ilike(f'%{keyword}%'))

        total = query.count()
        rows = (
            query
            .order_by(AdventurerS1Tombstone.died_at.desc(), AdventurerS1Tombstone.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            'items': S1TombstoneService._attach_message_meta(rows),
            'total': total,
            'page': page,
            'page_size': page_size,
            'has_more': page * page_size < total,
        }, None

    @staticmethod
    def list_messages(
        building_instance_id: int,
        tombstone_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Optional[dict], Optional[str]]:
        _, err = S1TombstoneService._resolve_memorial_instance(building_instance_id)
        if err:
            return None, err
        stone, err = S1TombstoneService._resolve_tombstone(tombstone_id)
        if err:
            return None, err

        page = max(1, int(page or 1))
        page_size = min(50, max(1, int(page_size or 20)))
        query = AdventurerS1TombstoneMessage.query.filter_by(tombstone_id=stone.id)
        total = query.count()
        rows = (
            query
            .options(joinedload(AdventurerS1TombstoneMessage.user))
            .order_by(AdventurerS1TombstoneMessage.created_at.asc(), AdventurerS1TombstoneMessage.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            'items': [row.to_dict() for row in rows],
            'total': total,
            'page': page,
            'page_size': page_size,
            'has_more': page * page_size < total,
        }, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def post_message(
        building_instance_id: int,
        tombstone_id: int,
        user_id: int,
        content: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        _, err = S1TombstoneService._resolve_memorial_instance(building_instance_id)
        if err:
            return None, err
        stone, err = S1TombstoneService._resolve_tombstone(tombstone_id)
        if err:
            return None, err

        content = (content or '').strip()
        if not content:
            return None, '留言不能为空'
        max_len = int(current_app.config.get('ADVENTURER_S1_TOMBSTONE_MESSAGE_MAX_LENGTH', 200))
        if len(content) > max_len:
            return None, f'留言不能超过 {max_len} 字'

        cooldown = int(current_app.config.get('ADVENTURER_S1_TOMBSTONE_MESSAGE_COOLDOWN_SECONDS', 10))
        if cooldown > 0:
            last = (
                AdventurerS1TombstoneMessage.query
                .filter_by(user_id=user_id, tombstone_id=stone.id)
                .order_by(AdventurerS1TombstoneMessage.created_at.desc())
                .first()
            )
            if last and last.created_at:
                elapsed = (_portable_utc_now() - last.created_at).total_seconds()
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed) + 1
                    return None, f'留言太频繁，请 {remaining} 秒后再试'

        msg = AdventurerS1TombstoneMessage(
            tombstone_id=stone.id,
            user_id=user_id,
            content=content,
        )
        db_manager.add(msg)
        db_manager.flush()
        loaded = (
            AdventurerS1TombstoneMessage.query
            .options(joinedload(AdventurerS1TombstoneMessage.user))
            .filter_by(id=msg.id)
            .first()
        )
        return loaded.to_dict() if loaded else msg.to_dict(), None

    @staticmethod
    def list_tributes(
        building_instance_id: int,
        tombstone_id: int,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """分页查询墓碑献花记录 + 按品种汇总。"""
        _, err = S1TombstoneService._resolve_memorial_instance(building_instance_id)
        if err:
            return None, err
        stone, err = S1TombstoneService._resolve_tombstone(tombstone_id)
        if err:
            return None, err

        page = max(1, int(page or 1))
        page_size = min(50, max(1, int(page_size or 20)))
        active_tribute_filter = S1TombstoneService._active_tribute_filter()
        query = (
            AdventurerS1TombstoneTribute.query
            .filter_by(tombstone_id=stone.id)
            .filter(active_tribute_filter)
        )
        total = query.count()
        rows = (
            query
            .options(
                joinedload(AdventurerS1TombstoneTribute.user),
                joinedload(AdventurerS1TombstoneTribute.adventurer),
            )
            .order_by(
                AdventurerS1TombstoneTribute.created_at.desc(),
                AdventurerS1TombstoneTribute.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # 按品种汇总
        summary_rows = (
            AdventurerS1TombstoneTribute.query
            .with_entities(
                AdventurerS1TombstoneTribute.flower_code,
                AdventurerS1TombstoneTribute.flower_name,
                func.sum(AdventurerS1TombstoneTribute.quantity),
            )
            .filter_by(tombstone_id=stone.id)
            .filter(active_tribute_filter)
            .group_by(
                AdventurerS1TombstoneTribute.flower_code,
                AdventurerS1TombstoneTribute.flower_name,
            )
            .all()
        )
        flower_summary = [
            {
                'code': fcode,
                'name': fname,
                'emoji': AdventurerS1TombstoneTribute._resolve_flower_emoji(fcode),
                'total_quantity': int(total_qty or 0),
            }
            for fcode, fname, total_qty in summary_rows
        ]

        return {
            'items': [row.to_dict() for row in rows],
            'total': total,
            'page': page,
            'page_size': page_size,
            'has_more': page * page_size < total,
            'flower_summary': flower_summary,
        }, None

    @staticmethod
    @with_transaction(auto_commit=True)
    def post_tribute(
        building_instance_id: int,
        tombstone_id: int,
        user_id: int,
        adventurer_id: int,
        flower_code: str,
        quantity: int,
        message: str | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """在鲜花店购买鲜花并直接送到墓碑；鲜花只用于 24 小时纪念展示。"""
        _, err = S1TombstoneService._resolve_memorial_instance(building_instance_id)
        if err:
            return None, err
        _, err = S1TombstoneService._resolve_flower_shop_instance()
        if err:
            return None, err
        stone, err = S1TombstoneService._resolve_tombstone(tombstone_id)
        if err:
            return None, err

        catalog = current_app.config.get('ADVENTURER_S1_FLOWER_SHOP_CATALOG') or {}
        entry = catalog.get(flower_code)
        if not entry:
            return None, '鲜花不存在'
        flower_name = entry.get('name', flower_code)
        unit_price = round(float(entry.get('price') or 0), 2)
        if unit_price <= 0:
            return None, '鲜花价格配置无效'

        quantity = int(quantity or 0)
        if quantity < 1:
            return None, '献花数量至少为 1'

        # 寄语长度校验
        message = (message or '').strip()
        if message:
            max_len = int(current_app.config.get(
                'ADVENTURER_S1_TOMBSTONE_TRIBUTE_MESSAGE_MAX_LENGTH', 100,
            ))
            if len(message) > max_len:
                return None, f'寄语不能超过 {max_len} 字'

        # 冒险者校验
        adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '只能让自己的冒险者献花'
        if adv.status in ('dead', 'archived'):
            return None, '阵亡或归档冒险者不能支付鲜花费用'

        # 冷却检查：同冒险者对同墓碑
        cooldown = int(current_app.config.get(
            'ADVENTURER_S1_TOMBSTONE_TRIBUTE_COOLDOWN_SECONDS', 10,
        ))
        if cooldown > 0:
            last = (
                AdventurerS1TombstoneTribute.query
                .filter_by(adventurer_id=adventurer_id, tombstone_id=stone.id)
                .order_by(AdventurerS1TombstoneTribute.created_at.desc())
                .first()
            )
            if last and last.created_at:
                elapsed = (_portable_utc_now() - last.created_at).total_seconds()
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed) + 1
                    return None, f'献花太频繁，请 {remaining} 秒后再试'

        total_price = round(unit_price * quantity, 2)
        remaining_gold, currency_err = S1CurrencyService.deduct(
            adventurer_id,
            'gold',
            total_price,
            source_type='tombstone_flower',
            related_id=stone.id,
            related_type='tombstone',
            description=f'给「{stone.name}」送出{flower_name} ×{quantity}',
            auto_commit=False,
        )
        if currency_err:
            return None, currency_err

        # 写入献花记录
        now = _portable_utc_now()
        lifetime_hours = max(1, int(current_app.config.get(
            'ADVENTURER_S1_TOMBSTONE_FLOWER_LIFETIME_HOURS', 24,
        )))
        tribute = AdventurerS1TombstoneTribute(
            tombstone_id=stone.id,
            user_id=user_id,
            adventurer_id=adventurer_id,
            flower_code=flower_code,
            flower_name=flower_name,
            quantity=quantity,
            unit_price=unit_price,
            total_price=total_price,
            message=message or None,
            expires_at=now + timedelta(hours=lifetime_hours),
            created_at=now,
        )
        db_manager.add(tribute)
        db_manager.flush()
        loaded = (
            AdventurerS1TombstoneTribute.query
            .options(
                joinedload(AdventurerS1TombstoneTribute.user),
                joinedload(AdventurerS1TombstoneTribute.adventurer),
            )
            .filter_by(id=tribute.id)
            .first()
        )
        result = loaded.to_dict() if loaded else tribute.to_dict()
        result['remaining_gold'] = remaining_gold
        return result, None

    @staticmethod
    def list_flower_catalog_and_inventory(
        user_id: int,
    ) -> Tuple[dict, Optional[str]]:
        """返回鲜花目录、鲜花店状态和可支付金币的本人冒险者。"""
        catalog = current_app.config.get('ADVENTURER_S1_FLOWER_SHOP_CATALOG') or {}
        catalog_list = [
            {
                'code': code,
                'name': entry.get('name', code),
                'emoji': entry.get('emoji', '💐'),
                'price': float(entry.get('price', 0)),
                'description': entry.get('description', ''),
            }
            for code, entry in catalog.items()
        ]

        _, shop_err = S1TombstoneService._resolve_flower_shop_instance()
        advs = (
            AdventurerS1.query
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1.status.notin_(('dead', 'archived')),
            )
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        gold_rows = (
            AdventurerS1Currency.query
            .filter(
                AdventurerS1Currency.adventurer_id.in_([adv.id for adv in advs]),
                AdventurerS1Currency.currency_code == 'gold',
            )
            .all()
            if advs else []
        )
        gold_map = {row.adventurer_id: round(float(row.amount or 0), 2) for row in gold_rows}
        adventurer_list = [
            {
                'id': adv.id,
                'name': adv.name,
                'status': adv.status,
                'gold': gold_map.get(adv.id, round(float(adv.gold or 0), 2)),
            }
            for adv in advs
        ]

        return {
            'catalog': catalog_list,
            'adventurers': adventurer_list,
            'shop_built': shop_err is None,
            'shop_message': shop_err,
            'flower_lifetime_hours': max(1, int(current_app.config.get(
                'ADVENTURER_S1_TOMBSTONE_FLOWER_LIFETIME_HOURS', 24,
            ))),
        }, None
