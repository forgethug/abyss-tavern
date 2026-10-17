"""Player-facing deterministic repair for historical S1 equipment pollution."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1ItemLog,
    AdventurerS1Settings,
    AdventurerS1Trait,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_existing_asset_update_contract import (
    preserve_s1_equipment_public_description,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of


class S1PlayerEquipmentIntegrityService:
    """Repair only equipment descendants that can be bound without inference.

    Historical expedition output may have replaced an equipment instance's
    immutable type/stage, dropped its existing public description, or copied
    another frozen equipment's exact identity onto the wrong stable ID.  This
    scanner replays frozen snapshots in chronological order and follows an ID
    change only when the complete persisted state has exactly one match.
    Ambiguous, missing, consumed, sold, or otherwise changed lineages remain
    read-only findings and are never repaired automatically.
    """

    SNAPSHOT_OPERATION = 'player_equipment_integrity_repair'
    BACKUP_KEY_PREFIX = 'player_eq_integrity:'
    PROTECTED_FIELDS = ('type', 'evolution_stage', 'custom_desc')
    IDENTITY_REPAIR_FIELDS = (
        'name',
        'type',
        'rarity',
        'custom_desc',
        'uses_left',
        'evolution_stage',
        'recovery_minutes',
    )

    @staticmethod
    def _text(value: object) -> str:
        return str(value or '').strip()

    @staticmethod
    def _integer(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _datetime(cls, value: object) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        else:
            raw_value = cls._text(value)
            if not raw_value:
                return None
            try:
                parsed = datetime.fromisoformat(raw_value)
            except ValueError:
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @classmethod
    def _backup_key(cls, user_id: int) -> str:
        return f'{cls.BACKUP_KEY_PREFIX}{int(user_id)}'

    @classmethod
    def _snapshot_state(cls, row: object) -> dict:
        return {
            'name': cls._text(getattr(row, 'name', '')),
            'type': cls._text(getattr(row, 'type', '')),
            'rarity': cls._text(getattr(row, 'rarity', None) or 'common'),
            'custom_desc': cls._text(getattr(row, 'custom_desc', None)),
            'uses_left': getattr(row, 'uses_left', None),
            'evolution_stage': cls._integer(
                getattr(row, 'evolution_stage', None),
            ),
            'quantity': item_quantity_of(row),
            'recovery_minutes': max(
                0,
                cls._integer(getattr(row, 'recovery_minutes', None)),
            ),
        }

    @classmethod
    def _frozen_state(cls, frozen: dict) -> dict:
        return {
            'name': cls._text(frozen.get('name')),
            'type': cls._text(frozen.get('type')),
            'rarity': cls._text(frozen.get('rarity') or 'common'),
            'custom_desc': cls._text(
                frozen.get('description') or frozen.get('custom_desc')
            ),
            'uses_left': frozen.get('uses_left'),
            'evolution_stage': cls._integer(frozen.get('evolution_stage')),
            'quantity': max(1, cls._integer(frozen.get('quantity'), 1)),
            'recovery_minutes': max(
                0,
                cls._integer(frozen.get('recovery_minutes')),
            ),
        }

    @classmethod
    def _settled_state(cls, frozen: dict, update: dict) -> dict:
        return {
            'name': cls._text(update.get('name')),
            'type': cls._text(update.get('type')),
            'rarity': cls._text(update.get('rarity') or 'common'),
            'custom_desc': cls._text(update.get('desc')),
            'uses_left': update.get('uses_left'),
            'evolution_stage': cls._integer(update.get('evolution_stage')),
            'quantity': max(1, cls._integer(frozen.get('quantity'), 1)),
            'recovery_minutes': max(
                0,
                cls._integer(update.get('recovery_minutes')),
            ),
        }

    @classmethod
    def _repair_polluted_state(
        cls,
        frozen: dict,
        settled: dict,
        *,
        restore_identity: bool = False,
    ) -> tuple[dict, list[str]]:
        if restore_identity:
            repaired = cls._frozen_state(frozen)
            return repaired, [
                field for field in cls.IDENTITY_REPAIR_FIELDS
                if settled.get(field) != repaired.get(field)
            ]

        repaired = dict(settled)
        repair_fields: list[str] = []
        frozen_state = cls._frozen_state(frozen)
        repaired_desc = preserve_s1_equipment_public_description(
            frozen_state['custom_desc'],
            settled.get('custom_desc'),
        )
        if settled.get('type') != frozen_state['type']:
            repaired['type'] = frozen_state['type']
            repair_fields.append('type')
        if settled.get('evolution_stage') != frozen_state['evolution_stage']:
            repaired['evolution_stage'] = frozen_state['evolution_stage']
            repair_fields.append('evolution_stage')
        if settled.get('custom_desc') != repaired_desc:
            repaired['custom_desc'] = repaired_desc
            repair_fields.append('custom_desc')
        return repaired, repair_fields

    @classmethod
    def _state_is_compatible(
        cls,
        observed: dict,
        polluted: dict,
        repaired: dict,
        *,
        repair_fields: Optional[list[str]] = None,
    ) -> bool:
        allowed_repaired_fields = set(
            cls.PROTECTED_FIELDS
            if repair_fields is None
            else repair_fields
        )
        for field, polluted_value in polluted.items():
            allowed = [polluted_value]
            if field in allowed_repaired_fields:
                repaired_value = repaired.get(field)
                if repaired_value != polluted_value:
                    allowed.append(repaired_value)
            if observed.get(field) not in allowed:
                return False
        return True

    @classmethod
    def _replay_update(
        cls,
        observed_before: dict,
        repaired_before: dict,
        settled_after: dict,
    ) -> dict:
        repaired_after = dict(settled_after)
        repaired_after['type'] = repaired_before.get('type')
        repaired_after['evolution_stage'] = max(
            cls._integer(repaired_before.get('evolution_stage')),
            cls._integer(settled_after.get('evolution_stage')),
        )

        observed_desc = cls._text(observed_before.get('custom_desc'))
        repaired_desc = cls._text(repaired_before.get('custom_desc'))
        settled_desc = cls._text(settled_after.get('custom_desc'))
        if (
            observed_desc
            and observed_desc != repaired_desc
            and settled_desc.count(observed_desc) == 1
        ):
            repaired_after['custom_desc'] = settled_desc.replace(
                observed_desc,
                repaired_desc,
                1,
            )
        else:
            repaired_after['custom_desc'] = (
                preserve_s1_equipment_public_description(
                    repaired_desc,
                    settled_desc,
                )
            )
        return repaired_after

    @classmethod
    def _identity_collision_asset_id(
        cls,
        *,
        source_row: dict,
        frozen_by_id: dict[int, dict],
        settled_name: object,
    ) -> tuple[Optional[int], bool]:
        """Return the unique sibling whose frozen name was copied to source."""
        source_id = cls._integer(source_row.get('asset_id'))
        source_name = cls._text(
            (source_row.get('frozen') or {}).get('name')
        )
        final_name = cls._text(settled_name)
        if source_id <= 0 or not final_name or final_name == source_name:
            return None, False
        matches = [
            equipment_id
            for equipment_id, row in frozen_by_id.items()
            if equipment_id != source_id
            and cls._text((row.get('frozen') or {}).get('name')) == final_name
        ]
        if len(matches) == 1:
            return int(matches[0]), False
        return None, len(matches) > 1

    @classmethod
    def _extract_log_view(
        cls,
        log: AdventurerS1ExpeditionLog,
        owned_ids: set[int],
    ) -> dict[int, dict]:
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        views: dict[int, dict] = {}
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            adventurer_id = cls._integer(snapshot.get('adventurer_id'))
            if adventurer_id not in owned_ids:
                continue
            rows: list[dict] = []
            for frozen in snapshot.get('equipment') or []:
                if not isinstance(frozen, dict):
                    continue
                asset_id = cls._integer(frozen.get('asset_id'))
                if asset_id <= 0:
                    continue
                rows.append({
                    'asset_id': asset_id,
                    'frozen': frozen,
                    'state': cls._frozen_state(frozen),
                })
            views[adventurer_id] = {
                'adventurer_name': cls._text(
                    snapshot.get('adventurer_name')
                ),
                'rows': rows,
                'updates': {},
            }

        for member_update in full_log.get('member_updates') or []:
            if not isinstance(member_update, dict):
                continue
            adventurer_id = cls._integer(member_update.get('id'))
            if adventurer_id not in owned_ids:
                continue
            view = views.setdefault(adventurer_id, {
                'adventurer_name': '',
                'rows': [],
                'updates': {},
            })
            for update in member_update.get('equipment_updates') or []:
                if not isinstance(update, dict):
                    continue
                equipment_id = cls._integer(update.get('equipment_id'))
                if equipment_id > 0:
                    view['updates'][equipment_id] = update
        return views

    @classmethod
    def _load_player_history(
        cls,
        user_id: int,
    ) -> tuple[list[AdventurerS1], list[AdventurerS1ExpeditionLog], bool]:
        adventurers = (
            db_manager.query(AdventurerS1)
            .filter(AdventurerS1.owner_id == int(user_id))
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        if not adventurers:
            return [], [], False
        adventurer_ids = [int(row.id) for row in adventurers]
        log_limit = max(
            1,
            int(current_app.config.get(
                'ADVENTURER_S1_PLAYER_EQUIPMENT_INTEGRITY_LOG_LIMIT',
                500,
            )),
        )
        member_log_ids = (
            db_manager.query(AdventurerS1ExpeditionLogMember.expedition_log_id)
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id.in_(adventurer_ids)
            )
            .distinct()
            .subquery()
        )
        logs_desc = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .join(
                member_log_ids,
                member_log_ids.c.expedition_log_id
                == AdventurerS1ExpeditionLog.id,
            )
            .filter(AdventurerS1ExpeditionLog.status == 'completed')
            .order_by(
                AdventurerS1ExpeditionLog.created_at.desc(),
                AdventurerS1ExpeditionLog.id.desc(),
            )
            .limit(log_limit + 1)
            .all()
        )
        truncated = len(logs_desc) > log_limit
        selected = logs_desc[:log_limit]
        selected.reverse()
        return adventurers, selected, truncated

    @classmethod
    def _current_records(cls, adventurer_ids: list[int]) -> list[dict]:
        if not adventurer_ids:
            return []
        equipment_rows = (
            db_manager.query(AdventurerS1Equipment)
            .filter(AdventurerS1Equipment.adventurer_id.in_(adventurer_ids))
            .order_by(AdventurerS1Equipment.id.asc())
            .all()
        )
        warehouse_rows = (
            db_manager.query(AdventurerS1WarehouseItem)
            .filter(AdventurerS1WarehouseItem.adventurer_id.in_(adventurer_ids))
            .order_by(AdventurerS1WarehouseItem.id.asc())
            .all()
        )
        records = [{
            'container': 'equipment',
            'row_id': int(row.id),
            'adventurer_id': int(row.adventurer_id),
            'state': cls._snapshot_state(row),
        } for row in equipment_rows]
        records.extend({
            'container': 'warehouse',
            'row_id': int(row.id),
            'adventurer_id': int(row.adventurer_id),
            'state': cls._snapshot_state(row),
        } for row in warehouse_rows)
        return records

    @classmethod
    def _load_active_appeal_audits(
        cls,
        adventurer_ids: list[int],
        lineage_refs: set[tuple[str, int]],
        *,
        earliest_source_at: Optional[datetime] = None,
    ) -> dict[tuple[int, str, int], list[datetime]]:
        if not adventurer_ids or not lineage_refs:
            return {}
        target_ids = sorted({row_id for _container, row_id in lineage_refs})
        query = db_manager.query(AdventurerS1DataAppealAuditLog).filter(
            AdventurerS1DataAppealAuditLog.adventurer_id.in_(adventurer_ids),
            AdventurerS1DataAppealAuditLog.target_type.in_(
                ('equipment', 'warehouse', 'item')
            ),
            AdventurerS1DataAppealAuditLog.target_id.in_(target_ids),
            AdventurerS1DataAppealAuditLog.op_type.in_(
                ('update', 'delete', 'create_item')
            ),
            AdventurerS1DataAppealAuditLog.reverted.is_(False),
        )
        if earliest_source_at is not None:
            query = query.filter(
                AdventurerS1DataAppealAuditLog.created_at
                > earliest_source_at
            )
        rows = query.order_by(
            AdventurerS1DataAppealAuditLog.created_at.asc(),
            AdventurerS1DataAppealAuditLog.id.asc(),
        ).all()
        audit_map: dict[tuple[int, str, int], list[datetime]] = {}
        for row in rows:
            if row.target_id is None or row.created_at is None:
                continue
            container = cls._text(row.target_type)
            if row.op_type == 'create_item' and container == 'item':
                after = row.after_json if isinstance(row.after_json, dict) else {}
                container = cls._text(after.get('destination'))
            if container not in ('equipment', 'warehouse'):
                continue
            key = (
                int(row.adventurer_id),
                container,
                int(row.target_id),
            )
            audit_map.setdefault(key, []).append(row.created_at)
        return audit_map

    @classmethod
    def _load_reverted_appeal_windows(
        cls,
        adventurer_ids: list[int],
        lineage_refs: set[tuple[str, int]],
        *,
        earliest_source_at: Optional[datetime] = None,
    ) -> dict[tuple[int, int], list[dict]]:
        if not adventurer_ids or not lineage_refs:
            return {}
        equipment_ids = sorted({
            row_id
            for container, row_id in lineage_refs
            if container == 'equipment'
        })
        if not equipment_ids:
            return {}
        query = db_manager.query(AdventurerS1DataAppealAuditLog).filter(
            AdventurerS1DataAppealAuditLog.adventurer_id.in_(adventurer_ids),
            AdventurerS1DataAppealAuditLog.target_type == 'equipment',
            AdventurerS1DataAppealAuditLog.target_id.in_(equipment_ids),
            AdventurerS1DataAppealAuditLog.op_type == 'update',
            AdventurerS1DataAppealAuditLog.reverted.is_(True),
            AdventurerS1DataAppealAuditLog.reverted_at.isnot(None),
        )
        if earliest_source_at is not None:
            query = query.filter(
                AdventurerS1DataAppealAuditLog.created_at
                > earliest_source_at
            )
        rows = query.order_by(
            AdventurerS1DataAppealAuditLog.created_at.asc(),
            AdventurerS1DataAppealAuditLog.id.asc(),
        ).all()
        windows: dict[tuple[int, int], list[dict]] = {}
        for row in rows:
            if (
                row.target_id is None
                or row.created_at is None
                or row.reverted_at is None
                or not isinstance(row.before_json, dict)
                or not isinstance(row.after_json, dict)
            ):
                continue
            after_record = dict(row.before_json)
            after_record.update(row.after_json)
            key = (int(row.adventurer_id), int(row.target_id))
            windows.setdefault(key, []).append({
                'created_at': row.created_at,
                'reverted_at': row.reverted_at,
                'before_state': cls._frozen_state(row.before_json),
                'after_state': cls._frozen_state(after_record),
            })
        return windows

    @classmethod
    def _is_reverted_appeal_snapshot(
        cls,
        reverted_windows: dict[tuple[int, int], list[dict]],
        *,
        adventurer_id: int,
        equipment_id: int,
        source_created_at: Optional[datetime],
        observation_created_at: Optional[datetime],
        observed_state: dict,
        polluted: dict,
        repaired: dict,
        repair_fields: list[str],
    ) -> bool:
        if source_created_at is None or observation_created_at is None:
            return False
        for window in reverted_windows.get(
            (int(adventurer_id), int(equipment_id)),
            [],
        ):
            created_at = window.get('created_at')
            reverted_at = window.get('reverted_at')
            if (
                created_at is None
                or reverted_at is None
                or not (
                    source_created_at
                    < created_at
                    <= observation_created_at
                    < reverted_at
                )
            ):
                continue
            if not cls._state_is_compatible(
                window.get('before_state') or {},
                polluted,
                repaired,
                repair_fields=repair_fields,
            ):
                continue
            if observed_state == window.get('after_state'):
                return True
        return False

    @classmethod
    def _has_post_source_appeal_audit(
        cls,
        audit_map: dict[tuple[int, str, int], list[datetime]],
        *,
        adventurer_id: int,
        lineage_refs: set[tuple[str, int]],
        source_created_at: Optional[datetime],
    ) -> bool:
        if source_created_at is None:
            return any(
                audit_map.get((int(adventurer_id), container, int(row_id)))
                for container, row_id in lineage_refs
            )
        return any(
            created_at > source_created_at
            for container, row_id in lineage_refs
            for created_at in audit_map.get(
                (int(adventurer_id), container, int(row_id)),
                [],
            )
        )

    @classmethod
    def _audit_map_for_snapshot_items(
        cls,
        items: list[dict],
    ) -> dict[tuple[int, str, int], list[datetime]]:
        adventurer_ids = sorted({
            cls._integer(item.get('adventurer_id'))
            for item in items
            if cls._integer(item.get('adventurer_id')) > 0
        })
        lineage_refs = {
            (cls._text(ref.get('container')), cls._integer(ref.get('row_id')))
            for item in items
            for ref in (item.get('lineage_refs') or [])
            if (
                isinstance(ref, dict)
                and cls._text(ref.get('container')) in ('equipment', 'warehouse')
                and cls._integer(ref.get('row_id')) > 0
            )
        }
        source_times = [
            source_time
            for item in items
            if (source_time := cls._datetime(item.get('source_created_at')))
            is not None
        ]
        return cls._load_active_appeal_audits(
            adventurer_ids,
            lineage_refs,
            earliest_source_at=min(source_times) if source_times else None,
        )

    @classmethod
    def _choose_observation(
        cls,
        records: list[dict],
        *,
        last_equipment_id: int,
        polluted: dict,
        repaired: dict,
        repair_fields: list[str],
        current_rows: bool = False,
    ) -> tuple[Optional[dict], str]:
        same_identifier = [
            row for row in records
            if row.get('asset_id', row.get('row_id')) == last_equipment_id
            and (not current_rows or row.get('container') == 'equipment')
        ]
        compatible = [
            row for row in records
            if cls._state_is_compatible(
                row['state'],
                polluted,
                repaired,
                repair_fields=repair_fields,
            )
        ]
        stable = [
            row for row in same_identifier
            if cls._state_is_compatible(
                row['state'],
                polluted,
                repaired,
                repair_fields=repair_fields,
            )
        ]
        if len(stable) == 1:
            return stable[0], 'stable_id'
        if same_identifier:
            return None, 'stable_id_changed'
        if len(compatible) == 1:
            return compatible[0], 'unique_state'
        if len(compatible) > 1:
            return None, 'ambiguous'
        return None, 'missing'

    @classmethod
    def _trace_declaration(
        cls,
        *,
        source_index: int,
        source_log: AdventurerS1ExpeditionLog,
        source_view: dict,
        source_row: dict,
        update: dict,
        log_views: list[dict[int, dict]],
        current_records: list[dict],
        appeal_audit_map: dict[tuple[int, str, int], list[datetime]],
        reverted_appeal_windows: dict[tuple[int, int], list[dict]],
        repair_kind: str,
        conflicting_frozen_equipment_id: Optional[int] = None,
    ) -> tuple[Optional[dict], str]:
        adventurer_id = cls._integer(source_view.get('adventurer_id'))
        frozen = source_row['frozen']
        polluted = cls._settled_state(frozen, update)
        restore_identity = repair_kind == 'identity_binding'
        if restore_identity:
            polluted['custom_desc'] = preserve_s1_equipment_public_description(
                frozen.get('description') or frozen.get('custom_desc'),
                polluted.get('custom_desc'),
            )
        repaired, source_repair_fields = cls._repair_polluted_state(
            frozen,
            polluted,
            restore_identity=restore_identity,
        )
        if not source_repair_fields:
            return None, 'not_polluted'
        last_equipment_id = source_row['asset_id']
        lineage_refs = {('equipment', int(last_equipment_id))}
        source_created_at = source_log.created_at

        for later_index in range(source_index + 1, len(log_views)):
            later_view = log_views[later_index].get(adventurer_id)
            if not later_view:
                continue
            rows = later_view.get('rows') or []
            observation, reason = cls._choose_observation(
                rows,
                last_equipment_id=last_equipment_id,
                polluted=polluted,
                repaired=repaired,
                repair_fields=source_repair_fields,
            )
            if observation is None:
                later_update = (later_view.get('updates') or {}).get(
                    last_equipment_id
                )
                same_identifier = [
                    row for row in rows
                    if cls._integer(row.get('asset_id'))
                    == last_equipment_id
                ]
                # 撤回申诉仍会留下一段不可变的探险冻结快照。只跨过审计
                # 生效窗口内、前后状态精确吻合且本轮无剧情更新的临时态。
                if (
                    reason == 'stable_id_changed'
                    and later_update is None
                    and len(same_identifier) == 1
                    and cls._is_reverted_appeal_snapshot(
                        reverted_appeal_windows,
                        adventurer_id=adventurer_id,
                        equipment_id=last_equipment_id,
                        source_created_at=source_created_at,
                        observation_created_at=later_view.get(
                            'log_created_at'
                        ),
                        observed_state=same_identifier[0]['state'],
                        polluted=polluted,
                        repaired=repaired,
                        repair_fields=source_repair_fields,
                    )
                ):
                    continue
                if reason in ('stable_id_changed', 'ambiguous'):
                    return None, (
                        'lineage_changed'
                        if reason == 'stable_id_changed'
                        else 'lineage_ambiguous'
                    )
                continue

            last_equipment_id = cls._integer(observation.get('asset_id'))
            lineage_refs.add(('equipment', int(last_equipment_id)))
            later_update = (later_view.get('updates') or {}).get(
                last_equipment_id
            )
            if later_update:
                if restore_identity:
                    # 完整身份错绑之后又发生新的专用修改时，无法机械区分
                    # 后续变化是在错误身份还是原身份上形成，保持只读。
                    return None, 'lineage_changed'
                observed_before = observation['state']
                polluted = cls._settled_state(
                    observation['frozen'],
                    later_update,
                )
                repaired = cls._replay_update(
                    observed_before,
                    repaired,
                    polluted,
                )

        if cls._has_post_source_appeal_audit(
            appeal_audit_map,
            adventurer_id=adventurer_id,
            lineage_refs=lineage_refs,
            source_created_at=source_created_at,
        ):
            return None, 'post_source_appeal_mutation'

        owned_current = [
            row for row in current_records
            if row['adventurer_id'] == adventurer_id
        ]
        current, reason = cls._choose_observation(
            owned_current,
            last_equipment_id=last_equipment_id,
            polluted=polluted,
            repaired=repaired,
            repair_fields=source_repair_fields,
            current_rows=True,
        )
        if current is None:
            return None, (
                'current_ambiguous' if reason == 'ambiguous'
                else 'current_missing_or_changed'
            )
        lineage_refs.add((current['container'], int(current['row_id'])))
        if cls._has_post_source_appeal_audit(
            appeal_audit_map,
            adventurer_id=adventurer_id,
            lineage_refs=lineage_refs,
            source_created_at=source_created_at,
        ):
            return None, 'post_source_appeal_mutation'

        current_state = current['state']
        repaired_state = dict(current_state)
        if restore_identity:
            for field in cls.IDENTITY_REPAIR_FIELDS:
                repaired_state[field] = repaired.get(field)
            repairable_fields = cls.IDENTITY_REPAIR_FIELDS
        else:
            repaired_state['type'] = repaired.get('type')
            repaired_state['evolution_stage'] = max(
                cls._integer(current_state.get('evolution_stage')),
                cls._integer(repaired.get('evolution_stage')),
            )
            repaired_state['custom_desc'] = repaired.get('custom_desc')
            repairable_fields = cls.PROTECTED_FIELDS
        repair_fields = [
            field for field in repairable_fields
            if current_state.get(field) != repaired_state.get(field)
        ]
        if not repair_fields:
            return None, 'already_repaired'
        return {
            'source_log_id': int(source_log.id),
            'adventurer_id': adventurer_id,
            'adventurer_name': cls._text(source_view.get('adventurer_name')),
            'container': current['container'],
            'row_id': current['row_id'],
            'equipment_name': current_state['name'],
            'source_equipment_id': source_row['asset_id'],
            'repair_kind': repair_kind,
            'conflicting_frozen_equipment_id': (
                conflicting_frozen_equipment_id
            ),
            'source_created_at': (
                source_created_at.isoformat() if source_created_at else ''
            ),
            'lineage_refs': [
                {'container': container, 'row_id': row_id}
                for container, row_id in sorted(lineage_refs)
            ],
            'current_state': current_state,
            'repaired_state': repaired_state,
            'repair_fields': repair_fields,
        }, 'candidate'

    @classmethod
    def _build_candidates(cls, user_id: int) -> tuple[list[dict], dict]:
        adventurers, logs, truncated = cls._load_player_history(user_id)
        adventurer_ids = [int(row.id) for row in adventurers]
        owned_ids = set(adventurer_ids)
        current_records = cls._current_records(adventurer_ids)
        log_views = [cls._extract_log_view(log, owned_ids) for log in logs]
        all_lineage_refs = {
            ('equipment', int(row['asset_id']))
            for views in log_views
            for view in views.values()
            for row in (view.get('rows') or [])
        }
        all_lineage_refs.update({
            (row['container'], int(row['row_id']))
            for row in current_records
        })
        source_times = [log.created_at for log in logs if log.created_at]
        appeal_audit_map = cls._load_active_appeal_audits(
            adventurer_ids,
            all_lineage_refs,
            earliest_source_at=min(source_times) if source_times else None,
        )
        reverted_appeal_windows = cls._load_reverted_appeal_windows(
            adventurer_ids,
            all_lineage_refs,
            earliest_source_at=min(source_times) if source_times else None,
        )
        adventurer_names = {int(row.id): cls._text(row.name) for row in adventurers}
        for log, view_by_adventurer in zip(logs, log_views):
            for adventurer_id, view in view_by_adventurer.items():
                view['adventurer_id'] = adventurer_id
                view['log_created_at'] = log.created_at
                if not view.get('adventurer_name'):
                    view['adventurer_name'] = adventurer_names.get(
                        adventurer_id,
                        '',
                    )

        scan = {
            'scanned_log_count': len(logs),
            'scan_truncated': truncated,
            'pollution_declaration_count': 0,
            'identity_binding_issues': 0,
            'identity_binding_ambiguous': 0,
            'candidate_count': 0,
            'already_repaired': 0,
            'lineage_ambiguous': 0,
            'lineage_changed': 0,
            'current_ambiguous': 0,
            'current_missing_or_changed': 0,
            'post_source_appeal_mutation': 0,
            'missing_frozen_asset': 0,
            'subsumed_declarations': 0,
        }
        candidates_by_target: dict[tuple[str, int], dict] = {}
        for source_index, (log, views) in enumerate(zip(logs, log_views)):
            for adventurer_id, view in views.items():
                frozen_by_id = {
                    row['asset_id']: row for row in view.get('rows') or []
                }
                for equipment_id, update in (view.get('updates') or {}).items():
                    source_row = frozen_by_id.get(equipment_id)
                    if source_row is None:
                        scan['missing_frozen_asset'] += 1
                        continue
                    settled = cls._settled_state(source_row['frozen'], update)
                    conflicting_id, collision_ambiguous = (
                        cls._identity_collision_asset_id(
                            source_row=source_row,
                            frozen_by_id=frozen_by_id,
                            settled_name=settled.get('name'),
                        )
                    )
                    if collision_ambiguous:
                        scan['identity_binding_ambiguous'] += 1
                        continue
                    repair_kind = (
                        'identity_binding'
                        if conflicting_id is not None
                        else 'protected_state'
                    )
                    if repair_kind == 'identity_binding':
                        settled['custom_desc'] = (
                            preserve_s1_equipment_public_description(
                                source_row['frozen'].get('description')
                                or source_row['frozen'].get('custom_desc'),
                                settled.get('custom_desc'),
                            )
                        )
                    _repair, fields = cls._repair_polluted_state(
                        source_row['frozen'],
                        settled,
                        restore_identity=(repair_kind == 'identity_binding'),
                    )
                    if not fields:
                        continue
                    scan['pollution_declaration_count'] += 1
                    if repair_kind == 'identity_binding':
                        scan['identity_binding_issues'] += 1
                    candidate, reason = cls._trace_declaration(
                        source_index=source_index,
                        source_log=log,
                        source_view=view,
                        source_row=source_row,
                        update=update,
                        log_views=log_views,
                        current_records=current_records,
                        appeal_audit_map=appeal_audit_map,
                        reverted_appeal_windows=reverted_appeal_windows,
                        repair_kind=repair_kind,
                        conflicting_frozen_equipment_id=conflicting_id,
                    )
                    if candidate is None:
                        if reason in scan:
                            scan[reason] += 1
                        continue
                    target_key = (
                        candidate['container'],
                        candidate['row_id'],
                    )
                    if target_key in candidates_by_target:
                        scan['subsumed_declarations'] += 1
                        if (
                            candidates_by_target[target_key].get('repair_kind')
                            != 'identity_binding'
                            and candidate.get('repair_kind')
                            == 'identity_binding'
                        ):
                            candidates_by_target[target_key] = candidate
                        continue
                    candidates_by_target[target_key] = candidate

        candidates = list(candidates_by_target.values())
        candidates.sort(key=lambda item: (
            item['adventurer_id'],
            item['container'],
            item['row_id'],
        ))
        scan['candidate_count'] = len(candidates)
        return candidates, scan

    @classmethod
    def _restore_available(cls, user_id: int) -> bool:
        setting = db_manager.query(AdventurerS1Settings).filter_by(
            key=cls._backup_key(user_id),
        ).first()
        if setting is None or not setting.value:
            return False
        try:
            payload = json.loads(setting.value)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get('status') == 'active'
            and cls._integer(payload.get('user_id')) == int(user_id)
            and bool(payload.get('entries'))
        )

    @classmethod
    def preview(cls, user_id: int):
        try:
            user_id = int(user_id)
            candidates, scan = cls._build_candidates(user_id)
            snapshot = {
                'user_id': user_id,
                'summary': scan,
                'items': candidates,
            }
            token = None
            if candidates:
                # This shared helper scopes tokens by operation + caller id;
                # no administrator authority is granted by using it here.
                token = S1AdminScanSnapshotService.store(
                    cls.SNAPSHOT_OPERATION,
                    user_id,
                    snapshot,
                )
            public_items = []
            for item in candidates:
                before = item['current_state']
                repaired = item['repaired_state']
                public_items.append({
                    'source_log_id': item['source_log_id'],
                    'adventurer_name': item['adventurer_name'],
                    'equipment_name': item['equipment_name'],
                    'container': item['container'],
                    'repair_kind': item.get('repair_kind', 'protected_state'),
                    'repair_fields': item['repair_fields'],
                    'before_name': before.get('name'),
                    'repair_name': repaired.get('name'),
                    'before_type': before.get('type'),
                    'repair_type': repaired.get('type'),
                    'before_rarity': before.get('rarity'),
                    'repair_rarity': repaired.get('rarity'),
                    'before_uses_left': before.get('uses_left'),
                    'repair_uses_left': repaired.get('uses_left'),
                    'before_evolution_stage': before.get('evolution_stage'),
                    'repair_evolution_stage': repaired.get('evolution_stage'),
                    'before_recovery_minutes': before.get('recovery_minutes'),
                    'repair_recovery_minutes': repaired.get('recovery_minutes'),
                    'before_description': cls._text(before.get('custom_desc')),
                    'repair_description': cls._text(repaired.get('custom_desc')),
                    'before_description_length': len(
                        cls._text(before.get('custom_desc'))
                    ),
                    'repair_description_length': len(
                        cls._text(repaired.get('custom_desc'))
                    ),
                })
            return {
                'summary': scan,
                'items': public_items,
                'preview_token': token,
                'execution_available': bool(token),
                'restore_available': cls._restore_available(user_id),
            }, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('玩家装备聚合自检 preview 失败')
            return None, player_operation_failure(exc, '装备自检失败')

    @classmethod
    def _lock_owned_adventurers(
        cls,
        user_id: int,
    ) -> dict[int, AdventurerS1]:
        rows = (
            db_manager.query(AdventurerS1)
            .filter(AdventurerS1.owner_id == int(user_id))
            .order_by(AdventurerS1.id.asc())
            .with_for_update()
            .all()
        )
        return {int(row.id): row for row in rows}

    @classmethod
    def _lock_target_rows(cls, items: list[dict]) -> dict[tuple[str, int], object]:
        equipment_ids = sorted({
            cls._integer(item.get('row_id'))
            for item in items
            if item.get('container') == 'equipment'
        })
        warehouse_ids = sorted({
            cls._integer(item.get('row_id'))
            for item in items
            if item.get('container') == 'warehouse'
        })
        equipment_rows = (
            db_manager.query(AdventurerS1Equipment)
            .filter(AdventurerS1Equipment.id.in_(equipment_ids))
            .order_by(AdventurerS1Equipment.id.asc())
            .with_for_update()
            .all()
            if equipment_ids else []
        )
        warehouse_rows = (
            db_manager.query(AdventurerS1WarehouseItem)
            .filter(AdventurerS1WarehouseItem.id.in_(warehouse_ids))
            .order_by(AdventurerS1WarehouseItem.id.asc())
            .with_for_update()
            .all()
            if warehouse_ids else []
        )
        locked = {
            ('equipment', int(row.id)): row for row in equipment_rows
        }
        locked.update({
            ('warehouse', int(row.id)): row for row in warehouse_rows
        })
        return locked

    @classmethod
    def _write_item_audit(
        cls,
        *,
        row: object,
        change_type: str,
        backup_id: str,
        repair_fields: list[str],
    ) -> None:
        db_manager.add(AdventurerS1ItemLog(
            adventurer_id=int(getattr(row, 'adventurer_id')),
            item_name=cls._text(getattr(row, 'name', '')),
            item_type=cls._text(getattr(row, 'type', '')),
            item_rarity=cls._text(getattr(row, 'rarity', None) or 'common'),
            change_type=change_type,
            quantity=item_quantity_of(row),
            source_type='data_integrity',
            related_id=backup_id,
            related_type='equipment_integrity',
            description=(
                '确定性装备自检：' + '、'.join(repair_fields)
            )[:200],
            hidden_from_player=True,
        ))

    @classmethod
    def _apply_repair_fields(
        cls,
        row: object,
        state: dict,
        repair_fields: list[str],
    ) -> None:
        allowed = set(cls.IDENTITY_REPAIR_FIELDS)
        for field in repair_fields:
            if field not in allowed:
                raise ValueError(f'装备修复字段不受支持：{field}')
            value = state.get(field)
            if field == 'custom_desc':
                row.custom_desc = value or None
            else:
                setattr(row, field, value)

    @staticmethod
    def _snapshot_trait_state(row: AdventurerS1Trait) -> dict:
        description_ai_at = getattr(row, 'description_ai_at', None)
        return {
            'name': str(row.name or ''),
            'description': str(row.description or ''),
            'description_ai': row.description_ai,
            'description_ai_at': (
                description_ai_at.isoformat()
                if isinstance(description_ai_at, datetime) else None
            ),
            'description_ai_source_hash': row.description_ai_source_hash,
            'equipped': getattr(row, 'equipped', True) is not False,
            'recovered_desc': row.recovered_desc,
            'asset_dependencies': deepcopy(row.asset_dependencies),
            'is_archived': bool(row.is_archived),
        }

    @classmethod
    def _apply_trait_state(cls, row: AdventurerS1Trait, state: dict) -> None:
        row.description = str(state.get('description') or '')
        row.description_ai = state.get('description_ai')
        row.description_ai_at = cls._datetime(state.get('description_ai_at'))
        row.description_ai_source_hash = state.get('description_ai_source_hash')
        row.equipped = state.get('equipped', True) is not False
        row.recovered_desc = state.get('recovered_desc')
        row.asset_dependencies = deepcopy(state.get('asset_dependencies'))

    @classmethod
    def _sync_trait_renames(
        cls,
        changes_by_adventurer: dict[int, list[dict]],
    ) -> tuple[list[dict], list[dict]]:
        if not changes_by_adventurer:
            return [], []
        from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
            sync_trait_asset_renames,
        )
        trait_rows = (
            db_manager.query(AdventurerS1Trait)
            .filter(
                AdventurerS1Trait.adventurer_id.in_(
                    sorted(changes_by_adventurer)
                ),
                AdventurerS1Trait.is_archived.is_(False),
            )
            .order_by(
                AdventurerS1Trait.adventurer_id.asc(),
                AdventurerS1Trait.id.asc(),
            )
            .with_for_update()
            .all()
        )
        traits_by_adventurer: dict[int, list[AdventurerS1Trait]] = {}
        before_by_id = {}
        for row in trait_rows:
            traits_by_adventurer.setdefault(int(row.adventurer_id), []).append(row)
            before_by_id[int(row.id)] = cls._snapshot_trait_state(row)
        for adventurer_id in sorted(changes_by_adventurer):
            sync_trait_asset_renames(
                adventurer_id,
                changes_by_adventurer[adventurer_id],
                traits=traits_by_adventurer.get(adventurer_id, []),
            )
        entries = []
        for row in trait_rows:
            before_state = before_by_id[int(row.id)]
            after_state = cls._snapshot_trait_state(row)
            entries.append({
                'trait_id': int(row.id),
                'adventurer_id': int(row.adventurer_id),
                'before_state': before_state,
                'repaired_state': after_state,
                'changed_by_repair': before_state != after_state,
            })
        scopes = [{
            'adventurer_id': adventurer_id,
            'active_trait_ids': sorted(
                int(row.id)
                for row in traits_by_adventurer.get(adventurer_id, [])
            ),
        } for adventurer_id in sorted(changes_by_adventurer)]
        return entries, scopes

    @classmethod
    def _invalidate_after_write(
        cls,
        user_id: int,
        adventurer_ids: set[int],
    ) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_home_service import (
            invalidate_s1_init_cache,
        )
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
            invalidate_my_activity_cache,
        )
        for adventurer_id in adventurer_ids:
            invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_my_activity_cache(user_id)
        invalidate_s1_init_cache(user_id)

    @classmethod
    def execute(cls, user_id: int, preview_token: object):
        user_id = cls._integer(user_id)
        snapshot = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            user_id,
            preview_token,
        )
        if (
            snapshot is None
            or cls._integer(snapshot.get('user_id')) != user_id
        ):
            return None, '预览快照已过期，请重新自检后再修复'
        items = snapshot.get('items') or []
        if not items:
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION,
                user_id,
                preview_token,
            )
            return {
                'repaired_count': 0,
                'skipped_state_drifted': 0,
                'restore_available': cls._restore_available(user_id),
            }, None

        repaired_count = 0
        skipped = 0
        affected_adventurers: set[int] = set()
        rename_changes_by_adventurer: dict[int, list[dict]] = {}
        backup_id = f'{user_id}-{_portable_utc_now().strftime("%Y%m%d%H%M%S%f")}'
        backup = {
            'backup_id': backup_id,
            'user_id': user_id,
            'status': 'active',
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'entries': [],
            'trait_entries': [],
            'trait_scopes': [],
        }
        try:
            with db_manager.transaction():
                owned = cls._lock_owned_adventurers(user_id)
                locked = cls._lock_target_rows(items)
                appeal_audit_map = cls._audit_map_for_snapshot_items(items)
                for item in items:
                    key = (item.get('container'), cls._integer(item.get('row_id')))
                    row = locked.get(key)
                    adventurer_id = cls._integer(item.get('adventurer_id'))
                    if (
                        row is None
                        or adventurer_id not in owned
                        or int(getattr(row, 'adventurer_id')) != adventurer_id
                        or cls._snapshot_state(row) != item.get('current_state')
                        or cls._has_post_source_appeal_audit(
                            appeal_audit_map,
                            adventurer_id=adventurer_id,
                            lineage_refs={
                                (
                                    cls._text(ref.get('container')),
                                    cls._integer(ref.get('row_id')),
                                )
                                for ref in (item.get('lineage_refs') or [])
                                if isinstance(ref, dict)
                            },
                            source_created_at=cls._datetime(
                                item.get('source_created_at')
                            ),
                        )
                    ):
                        skipped += 1
                        continue
                    before_state = cls._snapshot_state(row)
                    repaired_state = item.get('repaired_state') or {}
                    repair_fields = list(item.get('repair_fields') or [])
                    cls._apply_repair_fields(
                        row,
                        repaired_state,
                        repair_fields,
                    )
                    actual_repaired_state = cls._snapshot_state(row)
                    if before_state.get('name') != actual_repaired_state.get('name'):
                        rename_changes_by_adventurer.setdefault(
                            adventurer_id,
                            [],
                        ).append({
                            'asset_kind': 'equipment',
                            'asset_id': int(getattr(row, 'id')),
                            'asset_location': item.get('container'),
                            'old_name': before_state.get('name'),
                            'new_name': actual_repaired_state.get('name'),
                        })
                    backup['entries'].append({
                        'container': item.get('container'),
                        'row_id': int(getattr(row, 'id')),
                        'adventurer_id': adventurer_id,
                        'before_state': before_state,
                        'repaired_state': actual_repaired_state,
                        'repair_fields': repair_fields,
                    })
                    cls._write_item_audit(
                        row=row,
                        change_type='repair',
                        backup_id=backup_id,
                        repair_fields=repair_fields,
                    )
                    affected_adventurers.add(adventurer_id)
                    repaired_count += 1

                (
                    backup['trait_entries'],
                    backup['trait_scopes'],
                ) = cls._sync_trait_renames(
                    rename_changes_by_adventurer
                )
                if backup['entries']:
                    setting = (
                        db_manager.query(AdventurerS1Settings)
                        .filter_by(key=cls._backup_key(user_id))
                        .with_for_update()
                        .first()
                    )
                    payload = json.dumps(backup, ensure_ascii=False)
                    if setting is None:
                        db_manager.add(AdventurerS1Settings(
                            key=cls._backup_key(user_id),
                            value=payload,
                            description='玩家确定性装备聚合修复备份',
                            updated_by_user_id=user_id,
                        ))
                    else:
                        setting.value = payload
                        setting.updated_by_user_id = user_id
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION,
                user_id,
                preview_token,
            )
            if affected_adventurers:
                cls._invalidate_after_write(
                    user_id,
                    affected_adventurers,
                )
            return {
                'repaired_count': repaired_count,
                'skipped_state_drifted': skipped,
                'restore_available': bool(backup['entries']),
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('玩家装备聚合自检 execute 失败')
            return None, player_operation_failure(exc, '装备修复失败')

    @classmethod
    def restore(cls, user_id: int):
        user_id = cls._integer(user_id)
        key = cls._backup_key(user_id)
        setting = db_manager.query(AdventurerS1Settings).filter_by(
            key=key,
        ).first()
        if setting is None or not setting.value:
            return None, '没有可撤销的装备自检修复'
        original_value = setting.value
        try:
            backup = json.loads(original_value)
        except (TypeError, json.JSONDecodeError):
            return None, '装备修复备份已损坏，无法撤销'
        if (
            not isinstance(backup, dict)
            or backup.get('status') != 'active'
            or cls._integer(backup.get('user_id')) != user_id
            or not isinstance(backup.get('entries'), list)
            or not backup['entries']
        ):
            return None, '没有可撤销的装备自检修复'

        entries = backup['entries']
        trait_entries = backup.get('trait_entries') or []
        trait_scopes = backup.get('trait_scopes') or []
        if (
            not isinstance(trait_entries, list)
            or not isinstance(trait_scopes, list)
            or any(not isinstance(entry, dict) for entry in trait_entries)
            or any(not isinstance(scope, dict) for scope in trait_scopes)
            or any(
                cls._integer(entry.get('trait_id')) <= 0
                or cls._integer(entry.get('adventurer_id')) <= 0
                or not isinstance(entry.get('before_state'), dict)
                or not isinstance(entry.get('repaired_state'), dict)
                for entry in trait_entries
            )
            or any(
                cls._integer(scope.get('adventurer_id')) <= 0
                or not isinstance(scope.get('active_trait_ids'), list)
                for scope in trait_scopes
            )
        ):
            return None, '装备修复备份已损坏，无法撤销'
        restored_count = 0
        skipped = 0
        affected_adventurers: set[int] = set()
        backup_id = cls._text(backup.get('backup_id'))
        try:
            with db_manager.transaction():
                owned = cls._lock_owned_adventurers(user_id)
                locked = cls._lock_target_rows(entries)
                expected_active_trait_ids: dict[int, set[int]] = {}
                malformed_trait_scopes: set[int] = set()
                for scope in trait_scopes:
                    if not isinstance(scope, dict):
                        continue
                    adventurer_id = cls._integer(scope.get('adventurer_id'))
                    raw_ids = scope.get('active_trait_ids')
                    if adventurer_id <= 0 or not isinstance(raw_ids, list):
                        malformed_trait_scopes.add(adventurer_id)
                        continue
                    normalized_ids = [cls._integer(value) for value in raw_ids]
                    if (
                        any(value <= 0 for value in normalized_ids)
                        or len(normalized_ids) != len(set(normalized_ids))
                        or adventurer_id in expected_active_trait_ids
                    ):
                        malformed_trait_scopes.add(adventurer_id)
                        continue
                    expected_active_trait_ids[adventurer_id] = set(normalized_ids)

                trait_entries_by_adventurer: dict[int, list[dict]] = {}
                for trait_entry in trait_entries:
                    if not isinstance(trait_entry, dict):
                        continue
                    adventurer_id = cls._integer(
                        trait_entry.get('adventurer_id')
                    )
                    trait_entries_by_adventurer.setdefault(
                        adventurer_id,
                        [],
                    ).append(trait_entry)
                for adventurer_id, grouped_entries in (
                    trait_entries_by_adventurer.items()
                ):
                    expected_active_trait_ids.setdefault(
                        adventurer_id,
                        {
                            cls._integer(entry.get('trait_id'))
                            for entry in grouped_entries
                        },
                    )

                strict_trait_adventurers = set(expected_active_trait_ids)
                trait_rows = (
                    db_manager.query(AdventurerS1Trait)
                    .filter(
                        AdventurerS1Trait.adventurer_id.in_(
                            sorted(strict_trait_adventurers)
                        )
                    )
                    .order_by(
                        AdventurerS1Trait.adventurer_id.asc(),
                        AdventurerS1Trait.id.asc(),
                    )
                    .with_for_update()
                    .all()
                    if strict_trait_adventurers else []
                )
                locked_traits = {int(row.id): row for row in trait_rows}
                locked_setting = (
                    db_manager.query(AdventurerS1Settings)
                    .filter_by(key=key)
                    .with_for_update()
                    .first()
                )
                if (
                    locked_setting is None
                    or locked_setting.value != original_value
                ):
                    raise ValueError('装备修复备份已变化，请重新打开专项项目')
                repair_created_at = cls._datetime(backup.get('created_at'))
                restore_refs = {
                    (
                        cls._text(entry.get('container')),
                        cls._integer(entry.get('row_id')),
                    )
                    for entry in entries
                }
                restore_audit_map = cls._load_active_appeal_audits(
                    sorted(owned),
                    restore_refs,
                    earliest_source_at=repair_created_at,
                )
                blocked_adventurers = {
                    adventurer_id
                    for adventurer_id in malformed_trait_scopes
                    if adventurer_id > 0
                }
                active_trait_ids_by_adventurer: dict[int, set[int]] = {
                    adventurer_id: set()
                    for adventurer_id in strict_trait_adventurers
                }
                for trait_row in trait_rows:
                    if not bool(trait_row.is_archived):
                        active_trait_ids_by_adventurer.setdefault(
                            int(trait_row.adventurer_id),
                            set(),
                        ).add(int(trait_row.id))
                for adventurer_id in strict_trait_adventurers:
                    if (
                        adventurer_id not in owned
                        or active_trait_ids_by_adventurer.get(
                            adventurer_id,
                            set(),
                        ) != expected_active_trait_ids.get(
                            adventurer_id,
                            set(),
                        )
                    ):
                        blocked_adventurers.add(adventurer_id)

                seen_trait_ids: set[int] = set()
                for trait_entry in trait_entries:
                    if not isinstance(trait_entry, dict):
                        blocked_adventurers.update(strict_trait_adventurers)
                        continue
                    adventurer_id = cls._integer(
                        trait_entry.get('adventurer_id')
                    )
                    trait_id = cls._integer(trait_entry.get('trait_id'))
                    trait_row = locked_traits.get(trait_id)
                    if (
                        trait_id <= 0
                        or trait_id in seen_trait_ids
                        or trait_row is None
                        or adventurer_id not in strict_trait_adventurers
                        or int(trait_row.adventurer_id) != adventurer_id
                        or cls._snapshot_trait_state(trait_row)
                        != trait_entry.get('repaired_state')
                    ):
                        blocked_adventurers.add(adventurer_id)
                    seen_trait_ids.add(trait_id)
                for adventurer_id, expected_ids in (
                    expected_active_trait_ids.items()
                ):
                    backed_up_ids = {
                        cls._integer(entry.get('trait_id'))
                        for entry in trait_entries_by_adventurer.get(
                            adventurer_id,
                            [],
                        )
                    }
                    if backed_up_ids != expected_ids:
                        blocked_adventurers.add(adventurer_id)

                checked_entries = []
                for entry in entries:
                    row = locked.get((
                        entry.get('container'),
                        cls._integer(entry.get('row_id')),
                    ))
                    adventurer_id = cls._integer(entry.get('adventurer_id'))
                    valid = not (
                        row is None
                        or adventurer_id not in owned
                        or int(getattr(row, 'adventurer_id')) != adventurer_id
                        or cls._snapshot_state(row) != entry.get('repaired_state')
                        or cls._has_post_source_appeal_audit(
                            restore_audit_map,
                            adventurer_id=adventurer_id,
                            lineage_refs={(
                                cls._text(entry.get('container')),
                                cls._integer(entry.get('row_id')),
                            )},
                            source_created_at=repair_created_at,
                        )
                    )
                    checked_entries.append((entry, row, adventurer_id, valid))
                    if not valid and adventurer_id in strict_trait_adventurers:
                        blocked_adventurers.add(adventurer_id)

                for entry, row, adventurer_id, valid in checked_entries:
                    if not valid or adventurer_id in blocked_adventurers:
                        skipped += 1
                        continue
                    before_state = entry.get('before_state') or {}
                    repair_fields = list(entry.get('repair_fields') or [])
                    cls._apply_repair_fields(
                        row,
                        before_state,
                        repair_fields,
                    )
                    cls._write_item_audit(
                        row=row,
                        change_type='restore',
                        backup_id=backup_id,
                        repair_fields=repair_fields,
                    )
                    affected_adventurers.add(adventurer_id)
                    restored_count += 1

                for trait_entry in trait_entries:
                    if not isinstance(trait_entry, dict):
                        continue
                    adventurer_id = cls._integer(
                        trait_entry.get('adventurer_id')
                    )
                    if (
                        adventurer_id in blocked_adventurers
                        or not bool(trait_entry.get('changed_by_repair'))
                    ):
                        continue
                    trait_row = locked_traits.get(
                        cls._integer(trait_entry.get('trait_id'))
                    )
                    cls._apply_trait_state(
                        trait_row,
                        trait_entry.get('before_state') or {},
                    )
                backup['status'] = 'restored'
                backup['restored_at'] = _portable_utc_now().isoformat() + 'Z'
                backup['restore_result'] = {
                    'restored_count': restored_count,
                    'skipped_state_drifted': skipped,
                }
                locked_setting.value = json.dumps(backup, ensure_ascii=False)
                locked_setting.updated_by_user_id = user_id
            if affected_adventurers:
                cls._invalidate_after_write(
                    user_id,
                    affected_adventurers,
                )
            return {
                'restored_count': restored_count,
                'skipped_state_drifted': skipped,
                'restore_available': False,
            }, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('玩家装备聚合自检 restore 失败')
            return None, player_operation_failure(exc, '撤销装备修复失败')
