"""百艺阁进阶、双投入制作与伙伴嬗变结果的限时撤回。"""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta
from typing import Optional

from flask import current_app

from db_repository import db_manager
from db_repository.exceptions import DatabaseError
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingUsageLog,
    AdventurerS1Equipment,
    AdventurerS1Skill,
    AdventurerS1WarehouseItem,
)
from utils.time_utils import parse_iso_date, to_utc_iso


class _UndoRejected(Exception):
    """需要回滚事务并原样返回给玩家的业务拒绝。"""


class S1BuildingUndoService:
    """只允许撤回同类建筑最近一次成功结果；消费成本不会返还。"""

    SKILL_ADVANCE = 'skill_advance'
    BLACKSMITH = 'blacksmith'
    TAILOR_WORKSHOP = 'tailor_workshop'
    PET_EVOLUTION = 'pet_evolution'
    PET_EVOLUTION_STATE_FIELDS = (
        'name', 'type', 'rarity', 'custom_desc', 'uses_left',
        'damage_stage', 'evolution_stage', 'quantity', 'damage_note',
    )
    LEGACY_PET_EVOLUTION_STATE_FIELDS = (
        'name', 'custom_desc', 'rarity', 'evolution_stage',
    )

    @staticmethod
    def window_hours() -> int:
        return max(1, int(current_app.config.get(
            'ADVENTURER_S1_BUILDING_UNDO_WINDOW_HOURS', 24,
        )))

    @staticmethod
    def classify_result(result: object) -> Optional[str]:
        if not isinstance(result, dict) or not result.get('success'):
            return None
        if result.get('type') == 'skill_training' and result.get('operation') == 'advance':
            return S1BuildingUndoService.SKILL_ADVANCE
        if result.get('type') == 'blacksmith':
            return S1BuildingUndoService.BLACKSMITH
        if result.get('type') == 'tailor_workshop':
            return S1BuildingUndoService.TAILOR_WORKSHOP
        if result.get('type') == 'pet_evolution':
            return S1BuildingUndoService.PET_EVOLUTION
        return None

    @staticmethod
    def _has_complete_pet_evolution_snapshot(snapshot: object) -> bool:
        if not isinstance(snapshot, dict) or snapshot.get('kind') != S1BuildingUndoService.PET_EVOLUTION:
            return False
        before = snapshot.get('before')
        after = snapshot.get('after')
        required = set(S1BuildingUndoService.PET_EVOLUTION_STATE_FIELDS)
        return (
            snapshot.get('companion_id') is not None
            and isinstance(before, dict)
            and isinstance(after, dict)
            and required.issubset(before)
            and required.issubset(after)
        )

    @staticmethod
    def _legacy_pet_evolution_snapshot(result: object) -> Optional[dict]:
        """读取旧嬗变日志能证明的前后状态；存在新版快照时不降级兼容。"""
        if not isinstance(result, dict) or '_undo_snapshot' in result:
            return None
        before = result.get('pre_evolution_snapshot')
        if not isinstance(before, dict):
            return None
        required = set(S1BuildingUndoService.LEGACY_PET_EVOLUTION_STATE_FIELDS)
        if not required.issubset(before):
            return None
        try:
            companion_id = int(result.get('companion_id'))
            before_stage = int(before.get('evolution_stage'))
            after_stage = int(result.get('evolution_stage'))
        except (TypeError, ValueError):
            return None
        before_name = str(before.get('name') or '').strip()
        before_rarity = str(before.get('rarity') or '').strip()
        after_name = str(result.get('new_name') or '').strip()
        if not before_name or not before_rarity or not after_name:
            return None
        old_name = str(result.get('old_name') or '').strip()
        if old_name and old_name != before_name:
            return None
        return {
            'companion_id': companion_id,
            'before': {
                'name': before_name,
                'custom_desc': before.get('custom_desc'),
                'rarity': before_rarity,
                'evolution_stage': before_stage,
            },
            'after': {
                'name': after_name,
                'evolution_stage': after_stage,
            },
        }

    @staticmethod
    def latest_candidate_ids(adventurer_ids: list[int], now: Optional[datetime] = None) -> dict:
        """返回撤回窗口内每名冒险者、每种操作的最新成功日志 id。"""
        if not adventurer_ids:
            return {}
        now = now or _portable_utc_now()
        cutoff = now - timedelta(hours=S1BuildingUndoService.window_hours())
        rows = (
            AdventurerS1BuildingUsageLog.query
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1BuildingUsageLog.created_at >= cutoff,
            )
            .order_by(
                AdventurerS1BuildingUsageLog.created_at.desc(),
                AdventurerS1BuildingUsageLog.id.desc(),
            )
            .all()
        )
        latest = {}
        for row in rows:
            kind = S1BuildingUndoService.classify_result(row.result)
            key = (row.adventurer_id, kind)
            if kind and key not in latest:
                latest[key] = row.id
        return latest

    @staticmethod
    def public_meta(log: AdventurerS1BuildingUsageLog, latest_ids: dict, now: Optional[datetime] = None) -> dict:
        result = log.result if isinstance(log.result, dict) else {}
        kind = S1BuildingUndoService.classify_result(result)
        if not kind:
            return {}
        now = now or _portable_utc_now()
        expires_at = log.created_at + timedelta(hours=S1BuildingUndoService.window_hours())
        snapshot = result.get('_undo_snapshot')
        if kind == S1BuildingUndoService.SKILL_ADVANCE:
            has_snapshot = True
        elif kind == S1BuildingUndoService.PET_EVOLUTION:
            complete_snapshot = S1BuildingUndoService._has_complete_pet_evolution_snapshot(snapshot)
            legacy_snapshot = S1BuildingUndoService._legacy_pet_evolution_snapshot(result)
            has_snapshot = complete_snapshot or legacy_snapshot is not None
        else:
            has_snapshot = isinstance(snapshot, dict) and snapshot.get('kind') == kind
        available = bool(
            latest_ids.get((log.adventurer_id, kind)) == log.id
            and now <= expires_at
            and not isinstance(result.get('undo'), dict)
            and has_snapshot
        )
        meta = {
            'undo_kind': kind,
            'undo_available': available,
            'undo_expires_at': to_utc_iso(expires_at),
        }
        if kind == S1BuildingUndoService.PET_EVOLUTION and available:
            meta['undo_restore_mode'] = (
                'complete' if complete_snapshot else 'legacy_known_fields'
            )
        if kind in {
            S1BuildingUndoService.BLACKSMITH,
            S1BuildingUndoService.TAILOR_WORKSHOP,
        } and available:
            snapshot = result.get('_undo_snapshot')
            old_items = S1BuildingUndoService._blacksmith_old_items(snapshot)
            meta['undo_item_options'] = [{
                'index': index,
                'name': str(item.get('name') or ''),
                'type': str(item.get('type') or ''),
                'rarity': str(item.get('rarity') or 'common'),
            } for index, item in enumerate(old_items)]
        return meta

    @staticmethod
    def _blacksmith_old_items(snapshot: object) -> list[dict]:
        if not isinstance(snapshot, dict):
            return []
        old_items = snapshot.get('old_items')
        if isinstance(old_items, list):
            valid_items = [item for item in old_items if isinstance(item, dict)]
            if valid_items:
                return valid_items
        old_item = snapshot.get('old_item')
        return [old_item] if isinstance(old_item, dict) else []

    @staticmethod
    def _ensure_latest(log: AdventurerS1BuildingUsageLog, kind: str, now: datetime) -> Optional[str]:
        cutoff = now - timedelta(hours=S1BuildingUndoService.window_hours())
        rows = (
            AdventurerS1BuildingUsageLog.query
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id == log.adventurer_id,
                AdventurerS1BuildingUsageLog.created_at >= cutoff,
            )
            .order_by(
                AdventurerS1BuildingUsageLog.created_at.desc(),
                AdventurerS1BuildingUsageLog.id.desc(),
            )
            .with_for_update()
            .all()
        )
        latest = next(
            (row for row in rows if S1BuildingUndoService.classify_result(row.result) == kind),
            None,
        )
        if not latest or latest.id != log.id:
            return '只能撤回该类建筑最近一次成功结果'
        return None

    @staticmethod
    def _undo_skill(log: AdventurerS1BuildingUsageLog, result: dict) -> tuple[Optional[dict], Optional[str]]:
        old_skill = result.get('old_skill')
        new_skill = result.get('skill')
        if not isinstance(old_skill, dict) or not isinstance(new_skill, dict):
            return None, '该历史记录缺少技能快照，无法撤回'
        try:
            skill_id = int(new_skill.get('id'))
        except (TypeError, ValueError):
            return None, '该历史记录中的技能标识无效'
        skill = (
            AdventurerS1Skill.query
            .filter_by(id=skill_id, adventurer_id=log.adventurer_id)
            .with_for_update()
            .first()
        )
        if not skill:
            return None, '技能已不存在，无法撤回'
        current = (
            skill.name,
            skill.description or '',
            bool(skill.equipped),
            max(0, int(skill.recovery_minutes or 0)),
        )
        expected = (
            str(new_skill.get('name') or ''),
            str(new_skill.get('description') or ''),
            bool(new_skill.get('equipped', True)),
            max(0, int(new_skill.get('recovery_minutes') or 0)),
        )
        if current != expected:
            return None, '该技能在进阶后又发生了变化，无法安全撤回'

        old_name = str(old_skill.get('name') or '').strip()
        if not old_name:
            return None, '旧技能名称缺失，无法撤回'
        conflict = (
            AdventurerS1Skill.query
            .filter(
                AdventurerS1Skill.adventurer_id == log.adventurer_id,
                AdventurerS1Skill.name == old_name,
                AdventurerS1Skill.id != skill.id,
            )
            .with_for_update()
            .first()
        )
        if conflict:
            return None, f'当前已有同名技能「{old_name}」，无法撤回'

        skill.name = old_name
        skill.description = str(old_skill.get('description') or '')
        skill.equipped = bool(old_skill.get('equipped', True))
        skill.recovery_minutes = max(
            0, int(old_skill.get('recovery_minutes') or 0),
        )
        from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
            sync_trait_asset_renames,
        )
        sync_trait_asset_renames(log.adventurer_id, [{
            'asset_kind': 'skill',
            'asset_id': skill.id,
            'asset_location': 'skill',
            'old_name': expected[0],
            'new_name': old_name,
        }])
        return {'skill_id': skill.id, 'skill_name': skill.name}, None

    @staticmethod
    def _matching_output(adventurer_id: int, snapshot: dict):
        original_model = (
            AdventurerS1Equipment
            if snapshot.get('destination') == 'equipment'
            else AdventurerS1WarehouseItem
        )
        try:
            item_id = int(snapshot.get('id'))
        except (TypeError, ValueError):
            return None
        identity_filters = {
            'name': str(snapshot.get('name') or ''),
            'type': str(snapshot.get('type') or ''),
            'rarity': str(snapshot.get('rarity') or 'common'),
            'custom_desc': snapshot.get('custom_desc'),
            'uses_left': snapshot.get('uses_left'),
            'evolution_stage': int(snapshot.get('evolution_stage') or 0),
        }
        if 'recovery_minutes' in snapshot:
            identity_filters['recovery_minutes'] = max(
                0, int(snapshot.get('recovery_minutes') or 0),
            )
        if 'recovery_started_at' in snapshot:
            identity_filters['recovery_started_at'] = parse_iso_date(
                snapshot.get('recovery_started_at'),
            )
        if 'recovery_ends_at' in snapshot:
            identity_filters['recovery_ends_at'] = parse_iso_date(
                snapshot.get('recovery_ends_at'),
            )

        original = (
            original_model.query
            .filter_by(id=item_id, adventurer_id=adventurer_id)
            .with_for_update()
            .first()
        )
        if original is not None:
            return original if all(
                getattr(original, field, None) == expected
                for field, expected in identity_filters.items()
            ) else None

        # 随身与仓库分表，系统发车整理或玩家无损收纳都会重建行并更换 id。
        # 仅在完整业务字段仍一致、全局唯一且未合堆时追随该新实例；否则保持拒绝。
        relocated_candidates = []
        for candidate_model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            relocated_candidates.extend(
                candidate_model.query
                .filter_by(adventurer_id=adventurer_id, **identity_filters)
                .order_by(candidate_model.id.asc())
                .with_for_update()
                .all()
            )
        if len(relocated_candidates) != 1:
            return None
        relocated = relocated_candidates[0]
        if relocated.quantity != 1:
            return None
        return relocated

    @staticmethod
    def _undo_blacksmith(
        log: AdventurerS1BuildingUsageLog,
        result: dict,
        adv: AdventurerS1,
        item_index: Optional[int],
    ) -> tuple[Optional[dict], Optional[str]]:
        is_tailor = result.get('type') == S1BuildingUndoService.TAILOR_WORKSHOP
        action_label = '裁制' if is_tailor else '锻造'
        building_label = '绮罗裁坊' if is_tailor else '铁匠铺'
        snapshot = result.get('_undo_snapshot')
        if not isinstance(snapshot, dict):
            return None, f'该{action_label}缺少物品快照，无法安全撤回'
        old_items = S1BuildingUndoService._blacksmith_old_items(snapshot)
        new_item = snapshot.get('new_item')
        if not old_items or not isinstance(new_item, dict):
            return None, f'该{action_label}记录的物品快照不完整，无法撤回'
        if item_index is None:
            if len(old_items) > 1:
                return None, f'请选择要恢复的{action_label}投入物品'
            selected_index = 0
        elif isinstance(item_index, bool):
            return None, '要恢复的物品选项无效'
        else:
            try:
                selected_index = int(item_index)
            except (TypeError, ValueError):
                return None, '要恢复的物品选项无效'
        if selected_index < 0 or selected_index >= len(old_items):
            return None, '要恢复的物品选项不存在'
        old_item = old_items[selected_index]

        output = S1BuildingUndoService._matching_output(log.adventurer_id, new_item)
        if not output:
            return None, f'{action_label}成品已被使用、合并、移除或修改，无法安全撤回'
        if isinstance(output, AdventurerS1Equipment) and (output.damage_stage or output.damage_note):
            return None, f'{action_label}成品已产生损伤或变化，无法安全撤回'

        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_SYSTEM_REVOKE,
            consume_item,
        )
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item

        output_source = 'carry' if isinstance(output, AdventurerS1Equipment) else 'warehouse'
        _, consume_err = consume_item(
            adv.id, {'source': output_source, 'id': output.id}, amount=1,
            source_type='building_undo',
            description=f'撤回最近一次{building_label}{action_label}：收回成品',
            operation=ITEM_OPERATION_SYSTEM_REVOKE,
        )
        if consume_err:
            return None, consume_err

        if not str(old_item.get('name') or '').strip():
            return None, f'{action_label}投入物品快照不完整，无法撤回'
        source = old_item.get('source')
        force_destination = 'equipment' if source == 'carry' else 'warehouse'
        destination, grant_err = grant_raw_item(
            adv,
            str(old_item['name']).strip(),
            {
                'type': str(old_item.get('type') or ''),
                'rarity': str(old_item.get('rarity') or 'common'),
                'quantity': 1,
                'uses_left': old_item.get('uses_left'),
                'desc': old_item.get('custom_desc'),
                'evolution_stage': int(old_item.get('evolution_stage') or 0),
                'recovery_minutes': max(
                    0, int(old_item.get('recovery_minutes') or 0),
                ),
                'recovery_started_at': parse_iso_date(
                    old_item.get('recovery_started_at'),
                ),
                'recovery_ends_at': parse_iso_date(
                    old_item.get('recovery_ends_at'),
                ),
            },
            source_type='building_undo',
            description=f'撤回最近一次{building_label}{action_label}：恢复所选投入物品',
            force_destination=force_destination,
            allow_same_name_stack_merge=not bool(old_item.get('damage_stage') or old_item.get('damage_note')),
        )
        if grant_err:
            return None, grant_err
        if (old_item.get('damage_stage') or old_item.get('damage_note')) and destination == 'equipment':
            restored = (
                AdventurerS1Equipment.query
                .filter_by(
                    adventurer_id=adv.id,
                    name=str(old_item['name']).strip(),
                    type=str(old_item.get('type') or ''),
                    rarity=str(old_item.get('rarity') or 'common'),
                    custom_desc=old_item.get('custom_desc'),
                    uses_left=old_item.get('uses_left'),
                    evolution_stage=int(old_item.get('evolution_stage') or 0),
                    recovery_minutes=max(
                        0, int(old_item.get('recovery_minutes') or 0),
                    ),
                    recovery_started_at=parse_iso_date(
                        old_item.get('recovery_started_at'),
                    ),
                    recovery_ends_at=parse_iso_date(
                        old_item.get('recovery_ends_at'),
                    ),
                )
                .order_by(AdventurerS1Equipment.id.desc())
                .with_for_update()
                .first()
            )
            if restored:
                restored.damage_stage = old_item.get('damage_stage')
                restored.damage_note = old_item.get('damage_note')
        return {
            'restored_item': str(old_item['name']).strip(),
            'restored_item_index': selected_index,
            'materials_refunded': False,
        }, None

    @staticmethod
    def _undo_pet_evolution(
        log: AdventurerS1BuildingUsageLog,
        result: dict,
    ) -> tuple[Optional[dict], Optional[str]]:
        snapshot = result.get('_undo_snapshot')
        complete_snapshot = S1BuildingUndoService._has_complete_pet_evolution_snapshot(snapshot)
        legacy_snapshot = (
            None if complete_snapshot
            else S1BuildingUndoService._legacy_pet_evolution_snapshot(result)
        )
        if not complete_snapshot and legacy_snapshot is None:
            return None, '该嬗变记录缺少可验证的旧状态，无法安全撤回'
        if complete_snapshot:
            before = snapshot.get('before')
            after = snapshot.get('after')
            try:
                companion_id = int(snapshot.get('companion_id'))
                result_companion_id = int(result.get('companion_id'))
            except (TypeError, ValueError):
                return None, '该嬗变记录的伙伴标识无效，无法撤回'
            if companion_id != result_companion_id:
                return None, '该嬗变记录的伙伴标识不一致，无法撤回'
            restore_mode = 'complete'
        else:
            before = legacy_snapshot['before']
            after = legacy_snapshot['after']
            companion_id = legacy_snapshot['companion_id']
            restore_mode = 'legacy_known_fields'

        relocated = False
        companion = (
            AdventurerS1Equipment.query
            .filter_by(
                id=companion_id,
                adventurer_id=log.adventurer_id,
                type='companion',
            )
            .with_for_update()
            .first()
        )
        if not companion:
            carry_candidates = (
                AdventurerS1Equipment.query
                .filter_by(
                    adventurer_id=log.adventurer_id,
                    type='companion',
                    name=after['name'],
                    evolution_stage=after['evolution_stage'],
                )
                .order_by(AdventurerS1Equipment.id.asc())
                .with_for_update()
                .all()
            )
            warehouse_candidates = (
                AdventurerS1WarehouseItem.query
                .filter_by(
                    adventurer_id=log.adventurer_id,
                    type='companion',
                    name=after['name'],
                    evolution_stage=after['evolution_stage'],
                )
                .order_by(AdventurerS1WarehouseItem.id.asc())
                .with_for_update()
                .all()
            )
            relocated_candidates = carry_candidates + warehouse_candidates
            if not relocated_candidates:
                return None, '嬗变后的伙伴已被使用、改名、移除或不在随身/仓库，无法安全撤回'
            if len(relocated_candidates) != 1:
                return None, '当前存在多个同名同阶伙伴，无法确认嬗变原实例，请联系管理员处理'
            companion = relocated_candidates[0]
            if companion.quantity != 1:
                return None, '嬗变后的伙伴已与同类实例合并，无法确认原实例，请联系管理员处理'
            relocated = True
        if complete_snapshot:
            snapshot_fields = S1BuildingUndoService.PET_EVOLUTION_STATE_FIELDS
            if isinstance(companion, AdventurerS1WarehouseItem) and any(
                state.get(field) is not None
                for state in (before, after)
                for field in ('damage_stage', 'damage_note')
            ):
                return None, '该伙伴存入仓库时丢失了损伤信息，无法原样撤回，请联系管理员处理'
            current_state = {
                field: getattr(companion, field, None)
                for field in snapshot_fields
            }
            expected_state = {field: after.get(field) for field in snapshot_fields}
            if current_state != expected_state:
                warehouse_round_trip_damage_loss = bool(
                    relocated
                    and isinstance(companion, AdventurerS1Equipment)
                    and current_state['damage_stage'] is None
                    and current_state['damage_note'] is None
                    and all(
                        current_state[field] == expected_state[field]
                        for field in snapshot_fields
                        if field not in {'damage_stage', 'damage_note'}
                    )
                )
                if not warehouse_round_trip_damage_loss:
                    return None, '该伙伴在嬗变后又发生了变化，无法安全撤回'
            restored_state = {field: before.get(field) for field in snapshot_fields}
            if (
                not str(restored_state['name'] or '').strip()
                or not str(restored_state['type'] or '').strip()
                or not str(restored_state['rarity'] or '').strip()
            ):
                return None, '该嬗变记录的嬗变前快照不完整，无法撤回'
        else:
            expected_state = {
                'name': after['name'],
                'evolution_stage': after['evolution_stage'],
            }
            current_state = {
                'name': companion.name,
                'evolution_stage': companion.evolution_stage,
            }
            if current_state != expected_state:
                return None, '该伙伴的当前形态或阶级已与旧嬗变结果不同，无法安全撤回'
            restored_state = {
                field: before[field]
                for field in S1BuildingUndoService.LEGACY_PET_EVOLUTION_STATE_FIELDS
            }
        for field, value in restored_state.items():
            if hasattr(type(companion), field):
                setattr(companion, field, value)
        from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
            sync_trait_asset_renames,
        )
        sync_trait_asset_renames(log.adventurer_id, [{
            'asset_kind': 'equipment',
            'asset_id': companion.id,
            'asset_location': (
                'warehouse'
                if isinstance(companion, AdventurerS1WarehouseItem)
                else 'equipment'
            ),
            'old_name': str(after.get('name') or '').strip(),
            'new_name': str(companion.name or '').strip(),
        }])
        return {
            'companion_id': companion.id,
            'restored_companion': companion.name,
            'restore_mode': restore_mode,
            'restored_fields': list(restored_state),
            'materials_refunded': False,
        }, None

    @staticmethod
    def undo(
        user_id: int,
        log_id: int,
        item_index: Optional[int] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        now = _portable_utc_now()
        try:
            with db_manager.transaction():
                log = AdventurerS1BuildingUsageLog.query.filter_by(id=log_id).first()
                if not log:
                    return None, '建筑使用记录不存在'
                adv = AdventurerS1.query.filter_by(id=log.adventurer_id).with_for_update().first()
                if not adv or adv.owner_id != user_id:
                    return None, '无权撤回该记录'
                log = AdventurerS1BuildingUsageLog.query.filter_by(id=log_id).with_for_update().first()
                result = dict(log.result) if isinstance(log.result, dict) else {}
                kind = S1BuildingUndoService.classify_result(result)
                if not kind:
                    return None, '该记录不支持撤回'
                if isinstance(result.get('undo'), dict):
                    return None, '该记录已经撤回'
                if now > log.created_at + timedelta(hours=S1BuildingUndoService.window_hours()):
                    return None, f'已超过 {S1BuildingUndoService.window_hours()} 小时撤回期限'
                latest_err = S1BuildingUndoService._ensure_latest(log, kind, now)
                if latest_err:
                    return None, latest_err

                if kind == S1BuildingUndoService.SKILL_ADVANCE:
                    data, undo_err = S1BuildingUndoService._undo_skill(log, result)
                elif kind in {
                    S1BuildingUndoService.BLACKSMITH,
                    S1BuildingUndoService.TAILOR_WORKSHOP,
                }:
                    data, undo_err = S1BuildingUndoService._undo_blacksmith(
                        log, result, adv, item_index,
                    )
                else:
                    data, undo_err = S1BuildingUndoService._undo_pet_evolution(log, result)
                if undo_err:
                    raise _UndoRejected(undo_err)

                result['undo'] = {
                    'kind': kind,
                    'reverted_at': to_utc_iso(now),
                }
                if kind in {
                    S1BuildingUndoService.BLACKSMITH,
                    S1BuildingUndoService.TAILOR_WORKSHOP,
                } and data:
                    result['undo']['restored_item'] = data.get('restored_item')
                    result['undo']['restored_item_index'] = data.get('restored_item_index')
                elif kind == S1BuildingUndoService.PET_EVOLUTION and data:
                    result['undo']['companion_id'] = data.get('companion_id')
                    result['undo']['restored_companion'] = data.get('restored_companion')
                    result['undo']['restore_mode'] = data.get('restore_mode')
                    result['undo']['restored_fields'] = data.get('restored_fields')
                log.result = result

            from plugins.adventurer_s1.services.adventurer_s1_service import (
                invalidate_adventurer_detail_cache,
                invalidate_my_activity_cache,
            )
            invalidate_adventurer_detail_cache(log.adventurer_id)
            invalidate_my_activity_cache(user_id)
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {
                    'action': 'building_usage_undone',
                    'reason': 'building_usage_undo',
                    'ids': [log.adventurer_id],
                }, namespace='/')
            except Exception:
                pass
            return {
                'log_id': log.id,
                'adventurer_id': log.adventurer_id,
                'undo_kind': kind,
                'reverted_at': result['undo']['reverted_at'],
                **(data or {}),
            }, None
        except _UndoRejected as exc:
            return None, str(exc)
        except DatabaseError as exc:
            original = getattr(exc, 'original_error', None)
            if isinstance(original, _UndoRejected):
                return None, str(original)
            current_app.logger.exception('撤回建筑结果失败 log=%s user=%s', log_id, user_id)
            return None, '撤回失败，请稍后重试'
        except Exception as exc:
            current_app.logger.exception('撤回建筑结果失败 log=%s user=%s', log_id, user_id)
            return None, '撤回失败，请稍后重试'
