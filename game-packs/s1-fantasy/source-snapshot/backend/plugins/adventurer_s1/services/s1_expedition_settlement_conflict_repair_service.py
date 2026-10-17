"""Admin recovery for pre-settlement S1 stable-equipment settlement conflicts.

The repair deliberately has a narrow scope: an in-progress latest expedition may
remove a consumption declaration only when the same frozen, stable equipment ID
is also declared as an ``equipment_updates`` return-state update.  An explicit
admin preview/execute may also remove a one-unit ``new_equipments`` duplicate of
that stable ID's final name and retarget exact trait dependencies to the stable
ID.  The service never regenerates story text and never compensates player state
directly.
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import copy
import hashlib
import json
from collections import Counter
from datetime import datetime
from uuid import uuid4

from flask import current_app

from db_repository import db_manager
from db_repository.exceptions import DatabaseError
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1Party,
)
from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
    normalize_s1_consumed_equipment_payload,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_existing_asset_update_contract import (
    normalize_s1_existing_asset_updates,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import (
    item_quantity_of,
)
from plugins.adventurer_s1.services.s1_persistent_content_safety_service import (
    persistent_asset_state_hash,
)


class S1ExpeditionSettlementConflictRepairService:
    """Preview, repair, replay, and conditionally restore one stuck log."""

    VERSION = 1
    SNAPSHOT_OPERATION = 'expedition_settlement_conflict_repair_v1'
    AUDIT_KEY = '_settlement_conflict_repairs'
    ACTIVE_STATUSES = frozenset({'applied', 'replay_failed'})

    @staticmethod
    def _integer(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _text(value: object) -> str:
        return str(value or '').strip()

    @staticmethod
    def _now() -> str:
        return _portable_utc_now().isoformat(timespec='seconds') + 'Z'

    @classmethod
    def _digest(cls, value: object) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _semantic_log_hash(cls, full_log: object) -> str:
        payload = copy.deepcopy(full_log) if isinstance(full_log, dict) else {}
        payload.pop(cls.AUDIT_KEY, None)
        return cls._digest(payload)

    @staticmethod
    def _blocker(code: str, message: str, **context) -> dict:
        row = {'code': code, 'message': message}
        if context:
            row['context'] = context
        return row

    @classmethod
    def _audit_entries(cls, full_log: object) -> list[dict]:
        if not isinstance(full_log, dict):
            return []
        rows = full_log.get(cls.AUDIT_KEY)
        if not isinstance(rows, list):
            return []
        return [copy.deepcopy(row) for row in rows if isinstance(row, dict)]

    @classmethod
    def _find_repair(cls, full_log: object, repair_id: str) -> dict | None:
        target = cls._text(repair_id)
        for row in reversed(cls._audit_entries(full_log)):
            if cls._text(row.get('repair_id')) == target:
                return row
        return None

    @classmethod
    def _latest_active_repair(cls, full_log: object) -> dict | None:
        for row in reversed(cls._audit_entries(full_log)):
            if row.get('status') in cls.ACTIVE_STATUSES:
                return row
        return None

    @classmethod
    def _replace_repair_entry(cls, full_log: dict, replacement: dict) -> None:
        repair_id = cls._text(replacement.get('repair_id'))
        rows = cls._audit_entries(full_log)
        for index, row in enumerate(rows):
            if cls._text(row.get('repair_id')) == repair_id:
                rows[index] = copy.deepcopy(replacement)
                full_log[cls.AUDIT_KEY] = rows
                return
        raise ValueError('结算冲突修复审计记录不存在')

    @classmethod
    def _base_context(cls, log: AdventurerS1ExpeditionLog) -> tuple[dict, list[dict]]:
        blockers: list[dict] = []
        party = (
            db_manager.query(AdventurerS1Party).filter_by(id=log.party_id).first()
            if log.party_id else None
        )
        if not log.party_id or party is None:
            blockers.append(cls._blocker(
                'party_missing', '日志没有仍可核验的关联小队',
            ))
        elif party.status != 'exploring':
            blockers.append(cls._blocker(
                'party_not_exploring',
                f'关联小队状态为 {party.status}，不是 exploring',
            ))

        latest = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .filter_by(party_id=log.party_id)
            .order_by(
                AdventurerS1ExpeditionLog.created_at.desc(),
                AdventurerS1ExpeditionLog.id.desc(),
            )
            .first()
            if log.party_id else None
        )
        if latest is not None and int(latest.id) != int(log.id):
            blockers.append(cls._blocker(
                'not_latest_party_log', '该日志不是关联小队的最新日志',
            ))

        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        if full_log.get('settlement_receipt'):
            blockers.append(cls._blocker(
                'settlement_receipt_exists', '日志已经存在结算到账回执',
            ))
        return {
            'party_id': int(log.party_id) if log.party_id else None,
            'party_status': party.status if party is not None else None,
        }, blockers

    @classmethod
    def _snapshot_maps(cls, full_log: dict) -> tuple[dict[int, dict], list[dict]]:
        snapshots: dict[int, dict] = {}
        blockers: list[dict] = []
        for row in full_log.get('audit_member_snapshot') or []:
            if not isinstance(row, dict):
                continue
            adventurer_id = cls._integer(row.get('adventurer_id'))
            if adventurer_id <= 0:
                continue
            if adventurer_id in snapshots:
                blockers.append(cls._blocker(
                    'duplicate_member_snapshot',
                    f'冻结成员快照重复出现冒险者 #{adventurer_id}',
                    adventurer_id=adventurer_id,
                ))
                continue
            snapshots[adventurer_id] = row
        return snapshots, blockers

    @classmethod
    def _member_update_maps(
        cls,
        full_log: dict,
    ) -> tuple[dict[int, dict], dict[int, int], list[dict]]:
        updates: dict[int, dict] = {}
        indexes: dict[int, int] = {}
        blockers: list[dict] = []
        for index, row in enumerate(full_log.get('member_updates') or []):
            if not isinstance(row, dict):
                continue
            adventurer_id = cls._integer(row.get('id'))
            if adventurer_id <= 0:
                continue
            if adventurer_id in updates:
                blockers.append(cls._blocker(
                    'duplicate_member_update',
                    f'成员结算重复出现冒险者 #{adventurer_id}',
                    adventurer_id=adventurer_id,
                ))
                continue
            updates[adventurer_id] = row
            indexes[adventurer_id] = index
        return updates, indexes, blockers

    @classmethod
    def _snapshot_equipment_by_id(
        cls,
        snapshot: dict,
        adventurer_id: int,
    ) -> tuple[dict[int, dict], list[dict]]:
        rows: dict[int, dict] = {}
        blockers: list[dict] = []
        for item in snapshot.get('equipment') or []:
            if not isinstance(item, dict):
                continue
            equipment_id = cls._integer(item.get('asset_id'))
            if equipment_id <= 0:
                continue
            if equipment_id in rows:
                blockers.append(cls._blocker(
                    'duplicate_frozen_equipment_id',
                    f'冒险者 #{adventurer_id} 的冻结装备 ID {equipment_id} 重复',
                    adventurer_id=adventurer_id,
                    equipment_id=equipment_id,
                ))
                continue
            rows[equipment_id] = item
        return rows, blockers

    @classmethod
    def _new_equipment_names(cls, member_update: dict) -> set[str]:
        value = member_update.get('new_equipments')
        if not isinstance(value, dict):
            return set()
        return {
            cls._text(name)
            for name in value
            if cls._text(name)
        }

    @classmethod
    def _retarget_new_equipment_dependencies(
        cls,
        raw_traits: object,
        replacements: dict[str, int],
    ) -> object:
        """把管理员确认的同名重复新物依赖改绑到冻结稳定装备 ID。"""
        repaired = copy.deepcopy(raw_traits)
        if isinstance(repaired, dict):
            trait_rows = list(repaired.values())
        elif isinstance(repaired, list):
            trait_rows = repaired
        else:
            return repaired
        for trait in trait_rows:
            if not isinstance(trait, dict):
                continue
            dependencies = trait.get('asset_dependencies')
            if not isinstance(dependencies, list):
                continue
            for index, dependency in enumerate(dependencies):
                if not isinstance(dependency, dict):
                    continue
                name = cls._text(dependency.get('new_asset_name'))
                if (
                    cls._text(dependency.get('asset_kind')) == 'equipment'
                    and name in replacements
                ):
                    dependencies[index] = {
                        'asset_kind': 'equipment',
                        'asset_id': int(replacements[name]),
                    }
        return repaired

    @classmethod
    def _load_rows(
        cls,
        member_ids: set[int],
        equipment_ids: set[int],
        *,
        lock_rows: bool,
    ) -> tuple[dict[int, AdventurerS1], dict[int, AdventurerS1Equipment]]:
        adventurer_query = (
            db_manager.query(AdventurerS1)
            .filter(AdventurerS1.id.in_(sorted(member_ids)))
            .order_by(AdventurerS1.id.asc())
        )
        equipment_query = (
            db_manager.query(AdventurerS1Equipment)
            .filter(AdventurerS1Equipment.id.in_(sorted(equipment_ids)))
            .order_by(AdventurerS1Equipment.id.asc())
        )
        if lock_rows:
            adventurer_query = adventurer_query.populate_existing().with_for_update()
            equipment_query = equipment_query.populate_existing().with_for_update()
        adventurers = adventurer_query.all() if member_ids else []
        equipments = equipment_query.all() if equipment_ids else []
        return (
            {int(row.id): row for row in adventurers},
            {int(row.id): row for row in equipments},
        )

    @classmethod
    def _analyze_conflicts(
        cls,
        log: AdventurerS1ExpeditionLog,
        *,
        lock_rows: bool = False,
    ) -> dict:
        full_log = copy.deepcopy(log.full_log) if isinstance(log.full_log, dict) else {}
        snapshots, snapshot_blockers = cls._snapshot_maps(full_log)
        updates, update_indexes, update_blockers = cls._member_update_maps(full_log)
        blockers = [*snapshot_blockers, *update_blockers]

        equipment_ids = {
            cls._integer(entry.get('equipment_id'))
            for member_update in updates.values()
            for entry in (member_update.get('equipment_updates') or [])
            if isinstance(entry, dict) and cls._integer(entry.get('equipment_id')) > 0
        }
        adventurers, current_equipment = cls._load_rows(
            set(updates), equipment_ids, lock_rows=lock_rows,
        )
        all_new_names = {
            name
            for member_update in updates.values()
            for name in cls._new_equipment_names(member_update)
        }
        event_count = len(full_log.get('events') or [])
        conflict_count = 0
        candidates: list[dict] = []
        member_changes: list[dict] = []

        for adventurer_id, raw_member_update in updates.items():
            raw_equipment_updates = raw_member_update.get('equipment_updates')
            if not raw_equipment_updates:
                continue
            snapshot = snapshots.get(adventurer_id)
            if snapshot is None:
                blockers.append(cls._blocker(
                    'member_snapshot_missing',
                    f'冒险者 #{adventurer_id} 缺少冻结成员快照',
                    adventurer_id=adventurer_id,
                ))
                continue
            frozen_assets, asset_blockers = cls._snapshot_equipment_by_id(
                snapshot, adventurer_id,
            )
            blockers.extend(asset_blockers)

            normalized_update = copy.deepcopy(raw_member_update)
            contract_errors = normalize_s1_existing_asset_updates(
                normalized_update,
                member_asset_context={
                    'equipment_assets_by_id': frozen_assets,
                    'skill_assets_by_id': {},
                },
                event_count=event_count,
                path=f'member_updates.{update_indexes[adventurer_id]}',
                require_asset_context=True,
            )
            if contract_errors:
                blockers.append(cls._blocker(
                    'equipment_update_contract_invalid',
                    f'冒险者 #{adventurer_id} 的既有装备更新合同不完整',
                    adventurer_id=adventurer_id,
                    errors=contract_errors[:8],
                ))
                continue
            equipment_updates = normalized_update.get('equipment_updates') or []
            if not equipment_updates:
                continue

            raw_consumed = raw_member_update.get('consumed_equipments')
            raw_entries = (
                copy.deepcopy(raw_consumed)
                if isinstance(raw_consumed, list)
                else [copy.deepcopy(raw_consumed)] if raw_consumed is not None else []
            )
            source_quantities: Counter[str] = Counter()
            source_name_counts: Counter[str] = Counter()
            for frozen in frozen_assets.values():
                source_name = cls._text(frozen.get('name'))
                source_quantity = max(1, cls._integer(frozen.get('quantity'), 1))
                if source_name:
                    source_quantities[source_name] += source_quantity
                    source_name_counts[source_name] += 1

            renamed_away: Counter[str] = Counter()
            update_rows: list[tuple[dict, dict, str, str, int]] = []
            final_name_counts: Counter[str] = Counter()
            for equipment_update in equipment_updates:
                equipment_id = cls._integer(equipment_update.get('equipment_id'))
                frozen = frozen_assets.get(equipment_id) or {}
                source_name = cls._text(frozen.get('name'))
                final_name = cls._text(equipment_update.get('name'))
                source_quantity = max(1, cls._integer(frozen.get('quantity'), 1))
                if source_name and final_name and source_name != final_name:
                    renamed_away[source_name] += source_quantity
                if final_name:
                    final_name_counts[final_name] += 1
                update_rows.append((
                    equipment_update, frozen, source_name, final_name, source_quantity,
                ))

            post_update_names = set(all_new_names)
            for source_name, quantity in source_quantities.items():
                if quantity > renamed_away.get(source_name, 0):
                    post_update_names.add(source_name)
            post_update_names.update(final_name_counts)
            post_equipment_payload = {name: {} for name in post_update_names}

            normalized_entries: list[dict] = []
            for raw_index, raw_entry in enumerate(raw_entries):
                raw_rows = normalize_s1_consumed_equipment_payload([raw_entry])
                post_rows = normalize_s1_consumed_equipment_payload(
                    [raw_entry], post_equipment_payload,
                )
                normalized_entries.append({
                    'index': raw_index,
                    'raw': raw_rows[0] if len(raw_rows) == 1 else None,
                    'post': post_rows[0] if len(post_rows) == 1 else None,
                })
            raw_totals = Counter()
            post_totals = Counter()
            for row in normalized_entries:
                raw_row = row.get('raw') or {}
                post_row = row.get('post') or {}
                if cls._text(raw_row.get('name')):
                    raw_totals[cls._text(raw_row.get('name'))] += cls._integer(
                        raw_row.get('quantity')
                    )
                if cls._text(post_row.get('name')):
                    post_totals[cls._text(post_row.get('name'))] += cls._integer(
                        post_row.get('quantity')
                    )

            matched_indexes: dict[int, int] = {}
            member_candidates: list[dict] = []
            for equipment_update, frozen, source_name, final_name, source_quantity in update_rows:
                equipment_id = cls._integer(equipment_update.get('equipment_id'))
                remaining_source_quantity = max(
                    0,
                    source_quantities.get(source_name, 0)
                    - renamed_away.get(source_name, 0),
                )
                source_overconsumed = (
                    bool(source_name)
                    and raw_totals.get(source_name, 0) > remaining_source_quantity
                )
                final_consumed = (
                    bool(final_name) and post_totals.get(final_name, 0) > 0
                )
                if not source_overconsumed and not final_consumed:
                    continue
                conflict_count += 1

                matching_rows = []
                for normalized_entry in normalized_entries:
                    raw_row = normalized_entry.get('raw') or {}
                    post_row = normalized_entry.get('post') or {}
                    raw_name = cls._text(raw_row.get('name'))
                    post_name = cls._text(post_row.get('name'))
                    if (
                        (final_consumed and post_name == final_name)
                        or (source_overconsumed and raw_name == source_name)
                    ):
                        matching_rows.append(normalized_entry)

                adventurer = adventurers.get(adventurer_id)
                current = current_equipment.get(equipment_id)
                frozen_hash = cls._text(frozen.get('asset_state_hash'))
                current_hash = (
                    persistent_asset_state_hash('equipment', adventurer_id, current)
                    if current is not None else None
                )
                ambiguous_existing_name = (
                    source_name_counts.get(source_name, 0) != 1
                    or any(
                        cls._text(other.get('name')) == final_name
                        and other_id != equipment_id
                        for other_id, other in frozen_assets.items()
                    )
                    or final_name_counts.get(final_name, 0) != 1
                )
                unsafe_reason = None
                if adventurer is None or adventurer.status != 'exploring':
                    unsafe_reason = '冒险者当前不是 exploring 状态'
                elif not frozen_hash or current_hash != frozen_hash:
                    unsafe_reason = '当前装备状态与出发冻结哈希不一致'
                elif current is None or int(current.adventurer_id) != adventurer_id:
                    unsafe_reason = '稳定装备 ID 当前不属于该冒险者'
                elif cls._text(current.name) != source_name:
                    unsafe_reason = '稳定装备 ID 当前名称已漂移'
                elif source_quantity != 1 or item_quantity_of(current) != 1:
                    unsafe_reason = '仅支持冻结数量与当前数量都为 1 的稳定实例'
                elif ambiguous_existing_name:
                    unsafe_reason = '同名冻结实例或修改后名称无法唯一映射'
                elif source_name in all_new_names or final_name in all_new_names:
                    unsafe_reason = '同名新增物品可能表示跨成员转赠或重新取得'
                elif len(matching_rows) != 1:
                    unsafe_reason = '冲突消耗条目无法唯一定位'
                else:
                    matched = matching_rows[0]
                    raw_row = matched.get('raw') or {}
                    post_row = matched.get('post') or {}
                    if (
                        cls._integer(raw_row.get('quantity')) != 1
                        or cls._integer(post_row.get('quantity')) != 1
                    ):
                        unsafe_reason = '冲突消耗条目的数量不是 1'
                    elif matched['index'] in matched_indexes:
                        unsafe_reason = '同一消耗条目同时命中多个装备更新'

                if unsafe_reason:
                    blockers.append(cls._blocker(
                        'equipment_consumption_conflict_ambiguous',
                        f'装备 #{equipment_id} 的更新/消耗冲突不能机械修复：{unsafe_reason}',
                        adventurer_id=adventurer_id,
                        equipment_id=equipment_id,
                    ))
                    continue

                matched_index = int(matching_rows[0]['index'])
                matched_indexes[matched_index] = equipment_id
                post_row = matching_rows[0].get('post') or {}
                public_candidate = {
                    'repair_kind': 'remove_conflicting_consumption',
                    'adventurer_id': adventurer_id,
                    'adventurer_name': cls._text(snapshot.get('adventurer_name')),
                    'equipment_id': equipment_id,
                    'source_name': source_name,
                    'updated_name': final_name,
                    'consumed_name': cls._text(post_row.get('name')) or source_name,
                    'quantity': 1,
                }
                candidate = {
                    **public_candidate,
                    'matched_consumed_index': matched_index,
                    'asset_state_hash': frozen_hash,
                }
                candidate['candidate_digest'] = cls._digest(candidate)
                candidates.append(candidate)
                member_candidates.append(candidate)

            if member_candidates:
                indexes_to_remove = {
                    int(candidate['matched_consumed_index'])
                    for candidate in member_candidates
                }
                repaired_consumed = [
                    copy.deepcopy(value)
                    for index, value in enumerate(raw_entries)
                    if index not in indexes_to_remove
                ]
                member_changes.append({
                    'repair_kind': 'remove_conflicting_consumption',
                    'adventurer_id': adventurer_id,
                    'member_update_index': update_indexes[adventurer_id],
                    'original_consumed_equipments': copy.deepcopy(raw_consumed),
                    'repaired_consumed_equipments': repaired_consumed,
                    'equipment_ids': sorted(
                        int(candidate['equipment_id'])
                        for candidate in member_candidates
                    ),
                })

            raw_new_equipments = raw_member_update.get('new_equipments')
            member_new_names = cls._new_equipment_names(raw_member_update)
            duplicate_candidates: list[dict] = []
            for (
                equipment_update,
                frozen,
                source_name,
                final_name,
                source_quantity,
            ) in update_rows:
                if final_name not in member_new_names:
                    continue
                conflict_count += 1
                equipment_id = cls._integer(equipment_update.get('equipment_id'))
                remaining_source_quantity = max(
                    0,
                    source_quantities.get(source_name, 0)
                    - renamed_away.get(source_name, 0),
                )
                source_overconsumed = (
                    bool(source_name)
                    and raw_totals.get(source_name, 0) > remaining_source_quantity
                )
                final_consumed = (
                    bool(final_name) and post_totals.get(final_name, 0) > 0
                )
                adventurer = adventurers.get(adventurer_id)
                current = current_equipment.get(equipment_id)
                frozen_hash = cls._text(frozen.get('asset_state_hash'))
                current_hash = (
                    persistent_asset_state_hash('equipment', adventurer_id, current)
                    if current is not None else None
                )
                proposed_gain = (
                    raw_new_equipments.get(final_name)
                    if isinstance(raw_new_equipments, dict)
                    else None
                )
                proposed_quantity = (
                    cls._integer(proposed_gain.get('quantity'), 1)
                    if isinstance(proposed_gain, dict)
                    else 0
                )
                ambiguous_existing_name = (
                    source_name_counts.get(source_name, 0) != 1
                    or any(
                        cls._text(other.get('name')) == final_name
                        and other_id != equipment_id
                        for other_id, other in frozen_assets.items()
                    )
                    or final_name_counts.get(final_name, 0) != 1
                )
                unsafe_reason = None
                if source_overconsumed or final_consumed:
                    unsafe_reason = '同一稳定装备还存在更新/消耗冲突'
                elif adventurer is None or adventurer.status != 'exploring':
                    unsafe_reason = '冒险者当前不是 exploring 状态'
                elif not frozen_hash or current_hash != frozen_hash:
                    unsafe_reason = '当前装备状态与出发冻结哈希不一致'
                elif current is None or int(current.adventurer_id) != adventurer_id:
                    unsafe_reason = '稳定装备 ID 当前不属于该冒险者'
                elif cls._text(current.name) != source_name:
                    unsafe_reason = '稳定装备 ID 当前名称已漂移'
                elif source_quantity != 1 or item_quantity_of(current) != 1:
                    unsafe_reason = '仅支持冻结数量与当前数量都为 1 的稳定实例'
                elif ambiguous_existing_name:
                    unsafe_reason = '同名冻结实例或修改后名称无法唯一映射'
                elif not isinstance(proposed_gain, dict) or proposed_quantity != 1:
                    unsafe_reason = '仅支持完整字段对象且数量为 1 的同名新发放'

                if unsafe_reason:
                    blockers.append(cls._blocker(
                        'equipment_update_new_gain_conflict_ambiguous',
                        f'装备 #{equipment_id} 的更新/同名新发放冲突不能管理员确认修复：'
                        f'{unsafe_reason}',
                        adventurer_id=adventurer_id,
                        equipment_id=equipment_id,
                    ))
                    continue

                public_candidate = {
                    'repair_kind': 'remove_duplicate_new_after_stable_update',
                    'adventurer_id': adventurer_id,
                    'adventurer_name': cls._text(snapshot.get('adventurer_name')),
                    'equipment_id': equipment_id,
                    'source_name': source_name,
                    'updated_name': final_name,
                    'consumed_name': None,
                    'quantity': 1,
                    'semantic_confirmation_required': True,
                }
                candidate = {
                    **public_candidate,
                    'asset_state_hash': frozen_hash,
                }
                candidate['candidate_digest'] = cls._digest(candidate)
                candidates.append(candidate)
                duplicate_candidates.append(candidate)

            if duplicate_candidates:
                duplicate_names = {
                    str(candidate['updated_name'])
                    for candidate in duplicate_candidates
                }
                repaired_new_equipments = copy.deepcopy(raw_new_equipments)
                for duplicate_name in duplicate_names:
                    repaired_new_equipments.pop(duplicate_name, None)
                dependency_replacements = {
                    str(candidate['updated_name']): int(candidate['equipment_id'])
                    for candidate in duplicate_candidates
                }
                original_new_traits = copy.deepcopy(
                    raw_member_update.get('new_traits')
                )
                repaired_new_traits = cls._retarget_new_equipment_dependencies(
                    original_new_traits,
                    dependency_replacements,
                )
                member_changes.append({
                    'repair_kind': 'remove_duplicate_new_after_stable_update',
                    'adventurer_id': adventurer_id,
                    'member_update_index': update_indexes[adventurer_id],
                    'original_new_equipments': copy.deepcopy(raw_new_equipments),
                    'repaired_new_equipments': repaired_new_equipments,
                    'original_new_traits_present': 'new_traits' in raw_member_update,
                    'original_new_traits': original_new_traits,
                    'repaired_new_traits': repaired_new_traits,
                    'equipment_ids': sorted(
                        int(candidate['equipment_id'])
                        for candidate in duplicate_candidates
                    ),
                })

        if conflict_count and len(candidates) != conflict_count:
            blockers.append(cls._blocker(
                'conflicts_not_fully_repairable',
                '日志中的稳定装备结算冲突未能全部形成唯一修复项',
                conflict_count=conflict_count,
                candidate_count=len(candidates),
            ))

        return {
            'state_hash': cls._semantic_log_hash(full_log),
            'conflict_count': conflict_count,
            'candidates': candidates,
            'candidate_digests': sorted(
                candidate['candidate_digest'] for candidate in candidates
            ),
            'member_changes': member_changes,
            'blockers': blockers,
        }

    @classmethod
    def _public_candidates_from_marker(cls, marker: dict) -> list[dict]:
        result = []
        for candidate in marker.get('candidates') or []:
            if not isinstance(candidate, dict):
                continue
            projected = {
                key: candidate.get(key)
                for key in (
                    'repair_kind',
                    'adventurer_id', 'adventurer_name', 'equipment_id',
                    'source_name', 'updated_name', 'consumed_name', 'quantity',
                    'semantic_confirmation_required',
                )
            }
            projected['repair_kind'] = (
                cls._text(projected.get('repair_kind'))
                or 'remove_conflicting_consumption'
            )
            result.append(projected)
        return result

    @classmethod
    def _validate_marker_assets(
        cls,
        marker: dict,
        *,
        lock_rows: bool,
    ) -> list[dict]:
        candidates = [
            row for row in marker.get('candidates') or [] if isinstance(row, dict)
        ]
        member_ids = {
            cls._integer(row.get('adventurer_id')) for row in candidates
            if cls._integer(row.get('adventurer_id')) > 0
        }
        equipment_ids = {
            cls._integer(row.get('equipment_id')) for row in candidates
            if cls._integer(row.get('equipment_id')) > 0
        }
        adventurers, equipments = cls._load_rows(
            member_ids, equipment_ids, lock_rows=lock_rows,
        )
        blockers: list[dict] = []
        for candidate in candidates:
            adventurer_id = cls._integer(candidate.get('adventurer_id'))
            equipment_id = cls._integer(candidate.get('equipment_id'))
            adventurer = adventurers.get(adventurer_id)
            equipment = equipments.get(equipment_id)
            current_hash = (
                persistent_asset_state_hash('equipment', adventurer_id, equipment)
                if equipment is not None else None
            )
            if adventurer is None or adventurer.status != 'exploring':
                blockers.append(cls._blocker(
                    'member_state_drifted',
                    f'冒险者 #{adventurer_id} 当前状态已变化',
                ))
            elif (
                equipment is None
                or int(equipment.adventurer_id) != adventurer_id
                or current_hash != candidate.get('asset_state_hash')
            ):
                blockers.append(cls._blocker(
                    'equipment_state_drifted',
                    f'装备 #{equipment_id} 当前状态已变化',
                ))
        return blockers

    @classmethod
    def _preview_payload(
        cls,
        log: AdventurerS1ExpeditionLog,
        admin_user_id: int,
    ) -> dict:
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        base, blockers = cls._base_context(log)
        active = cls._latest_active_repair(full_log)
        if active is not None:
            state_hash = cls._semantic_log_hash(full_log)
            if log.status != 'in_progress':
                blockers.append(cls._blocker(
                    'log_not_in_progress',
                    f'日志状态为 {log.status}，不能继续重放或回滚',
                ))
            if state_hash != active.get('after_state_hash'):
                blockers.append(cls._blocker(
                    'repaired_log_state_drifted',
                    '修复后的日志内容已经变化，不能直接重放或回滚',
                ))
            blockers.extend(cls._validate_marker_assets(active, lock_rows=False))
            repairable = not blockers and log.status == 'in_progress'
            snapshot = {
                'version': cls.VERSION,
                'mode': 'retry',
                'log_id': int(log.id),
                'party_id': base.get('party_id'),
                'repair_id': active.get('repair_id'),
                'state_hash': state_hash,
            }
            token = (
                S1AdminScanSnapshotService.store(
                    cls.SNAPSHOT_OPERATION,
                    admin_user_id,
                    snapshot,
                    allow_signed_fallback=True,
                )
                if repairable else None
            )
            return {
                'log_id': int(log.id),
                'party_id': base.get('party_id'),
                'log_status': log.status,
                'mode': 'retry',
                'repair_id': active.get('repair_id'),
                'repairable': repairable,
                'can_restore': repairable,
                'candidate_count': len(active.get('candidates') or []),
                'candidates': cls._public_candidates_from_marker(active),
                'blockers': blockers,
                'last_replay_error': active.get('last_replay_error'),
                'preview_token': token,
                'execution_available': bool(token),
            }

        if log.status != 'in_progress':
            blockers.append(cls._blocker(
                'log_not_in_progress',
                f'日志状态为 {log.status}，只支持 in_progress 日志',
            ))
            return {
                'log_id': int(log.id),
                'party_id': base.get('party_id'),
                'log_status': log.status,
                'mode': 'none',
                'repairable': False,
                'can_restore': False,
                'candidate_count': 0,
                'candidates': [],
                'blockers': blockers,
                'preview_token': None,
                'execution_available': False,
            }

        analysis = cls._analyze_conflicts(log, lock_rows=False)
        blockers.extend(analysis['blockers'])
        repairable = bool(analysis['candidates']) and not blockers
        repair_id = uuid4().hex
        snapshot = {
            'version': cls.VERSION,
            'mode': 'apply',
            'log_id': int(log.id),
            'party_id': base.get('party_id'),
            'repair_id': repair_id,
            'state_hash': analysis['state_hash'],
            'candidate_digests': analysis['candidate_digests'],
        }
        token = (
            S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_OPERATION,
                admin_user_id,
                snapshot,
                allow_signed_fallback=True,
            )
            if repairable else None
        )
        return {
            'log_id': int(log.id),
            'party_id': base.get('party_id'),
            'log_status': log.status,
            'mode': 'apply' if analysis['conflict_count'] else 'none',
            'repair_id': repair_id if repairable else None,
            'repairable': repairable,
            'can_restore': False,
            'conflict_count': analysis['conflict_count'],
            'candidate_count': len(analysis['candidates']),
            'candidates': [
                {
                    key: candidate.get(key)
                    for key in (
                        'repair_kind',
                        'adventurer_id', 'adventurer_name', 'equipment_id',
                        'source_name', 'updated_name', 'consumed_name', 'quantity',
                        'semantic_confirmation_required',
                    )
                }
                for candidate in analysis['candidates']
            ],
            'blockers': blockers,
            'preview_token': token,
            'execution_available': bool(token),
        }

    @classmethod
    def preview(cls, expedition_log_id: int, admin_user_id: int):
        try:
            expedition_log_id = int(expedition_log_id)
            if expedition_log_id <= 0:
                raise ValueError('探险日志 ID 必须是正整数')
            log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(
                id=expedition_log_id,
            ).first()
            if log is None:
                return None, '探险日志不存在'
            return cls._preview_payload(log, int(admin_user_id)), None
        except ValueError as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 探险结算冲突恢复预览失败')
            return None, f'结算冲突恢复预览失败：{exc}'

    @classmethod
    def _capture_progress_state(cls, full_log: dict) -> dict:
        return {
            key: {
                'present': key in full_log,
                'value': copy.deepcopy(full_log.get(key)),
            }
            for key in ('current_event_index', 'progress_push_count', 'status')
        }

    @staticmethod
    def _restore_progress_state(full_log: dict, state: dict) -> None:
        for key in ('current_event_index', 'progress_push_count', 'status'):
            row = state.get(key) if isinstance(state, dict) else None
            if isinstance(row, dict) and row.get('present'):
                full_log[key] = copy.deepcopy(row.get('value'))
            else:
                full_log.pop(key, None)

    @classmethod
    def _apply_locked(
        cls,
        log: AdventurerS1ExpeditionLog,
        snapshot: dict,
        admin_user_id: int,
    ) -> tuple[str, int, bool]:
        repair_id = cls._text(snapshot.get('repair_id'))
        existing = cls._find_repair(log.full_log, repair_id)
        if existing is not None:
            full_log = log.full_log if isinstance(log.full_log, dict) else {}
            if log.status == 'completed' and full_log.get('settlement_receipt'):
                return repair_id, int(log.party_id), True
            if existing.get('status') not in cls.ACTIVE_STATUSES:
                raise ValueError('该修复批次已结束，不能再次执行')
            if cls._semantic_log_hash(log.full_log) != existing.get('after_state_hash'):
                raise ValueError('修复后的日志状态已变化，请重新预览')
            marker_blockers = cls._validate_marker_assets(existing, lock_rows=True)
            if marker_blockers:
                raise ValueError(marker_blockers[0]['message'])
            return repair_id, int(log.party_id), False

        if snapshot.get('mode') != 'apply':
            raise ValueError('修复预览模式与当前日志状态不一致')
        if log.status != 'in_progress':
            raise ValueError(f'日志状态已变为 {log.status}，不能应用修复')
        if cls._semantic_log_hash(log.full_log) != snapshot.get('state_hash'):
            raise ValueError('日志内容已变化，请重新预览')
        _base, base_blockers = cls._base_context(log)
        if base_blockers:
            raise ValueError(base_blockers[0]['message'])
        analysis = cls._analyze_conflicts(log, lock_rows=True)
        if analysis['blockers']:
            raise ValueError(analysis['blockers'][0]['message'])
        if not analysis['candidates']:
            raise ValueError('当前日志已没有可确认修复的稳定装备结算冲突')
        if analysis['candidate_digests'] != sorted(snapshot.get('candidate_digests') or []):
            raise ValueError('修复候选已变化，请重新预览')

        before_full_log = copy.deepcopy(log.full_log)
        repaired_full_log = copy.deepcopy(before_full_log)
        member_updates = repaired_full_log.get('member_updates') or []
        for change in analysis['member_changes']:
            index = cls._integer(change.get('member_update_index'), -1)
            if index < 0 or index >= len(member_updates):
                raise ValueError('修复成员结算索引已漂移')
            member_update = member_updates[index]
            if (
                not isinstance(member_update, dict)
                or cls._integer(member_update.get('id')) != change['adventurer_id']
            ):
                raise ValueError('修复成员结算字段已漂移')
            repair_kind = cls._text(change.get('repair_kind')) or (
                'remove_conflicting_consumption'
            )
            if repair_kind == 'remove_conflicting_consumption':
                if (
                    member_update.get('consumed_equipments')
                    != change.get('original_consumed_equipments')
                ):
                    raise ValueError('修复成员消耗字段已漂移')
                member_update['consumed_equipments'] = copy.deepcopy(
                    change['repaired_consumed_equipments']
                )
            elif repair_kind == 'remove_duplicate_new_after_stable_update':
                original_traits_present = bool(
                    change.get('original_new_traits_present')
                )
                if (
                    member_update.get('new_equipments')
                    != change.get('original_new_equipments')
                    or ('new_traits' in member_update) != original_traits_present
                    or (
                        original_traits_present
                        and member_update.get('new_traits')
                        != change.get('original_new_traits')
                    )
                ):
                    raise ValueError('修复成员同名新增或特质依赖字段已漂移')
                member_update['new_equipments'] = copy.deepcopy(
                    change['repaired_new_equipments']
                )
                if original_traits_present:
                    member_update['new_traits'] = copy.deepcopy(
                        change.get('repaired_new_traits')
                    )
            else:
                raise ValueError(f'未知的结算冲突修复类型：{repair_kind}')

        progress_before = cls._capture_progress_state(repaired_full_log)
        repaired_full_log['current_event_index'] = len(
            repaired_full_log.get('events') or []
        )
        before_hash = cls._semantic_log_hash(before_full_log)
        after_hash = cls._semantic_log_hash(repaired_full_log)
        marker = {
            'version': cls.VERSION,
            'repair_id': repair_id,
            'status': 'applied',
            'admin_user_id': int(admin_user_id),
            'created_at': cls._now(),
            'before_state_hash': before_hash,
            'after_state_hash': after_hash,
            'progress_before': progress_before,
            'member_changes': copy.deepcopy(analysis['member_changes']),
            'candidates': copy.deepcopy(analysis['candidates']),
            'replay_attempts': 0,
        }
        history = cls._audit_entries(repaired_full_log)
        history.append(marker)
        repaired_full_log[cls.AUDIT_KEY] = history
        log.full_log = repaired_full_log
        return repair_id, int(log.party_id), False

    @classmethod
    def _record_replay_outcome(
        cls,
        expedition_log_id: int,
        repair_id: str,
        *,
        admin_user_id: int,
        completed: bool,
        error_message: str | None,
    ) -> None:
        try:
            with db_manager.transaction():
                log = (
                    db_manager.query(AdventurerS1ExpeditionLog)
                    .filter_by(id=expedition_log_id)
                    .with_for_update()
                    .first()
                )
                if log is None:
                    return
                full_log = copy.deepcopy(log.full_log or {})
                marker = cls._find_repair(full_log, repair_id)
                if marker is None:
                    return
                marker['replay_attempts'] = cls._integer(
                    marker.get('replay_attempts')
                ) + 1
                marker['last_replay_at'] = cls._now()
                marker['last_replayed_by_user_id'] = int(admin_user_id)
                if completed:
                    marker['status'] = 'completed'
                    marker['completed_at'] = cls._now()
                    marker.pop('last_replay_error', None)
                else:
                    marker['status'] = 'replay_failed'
                    marker['last_replay_error'] = cls._text(error_message)[:1000]
                cls._replace_repair_entry(full_log, marker)
                log.full_log = full_log
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                'S1 探险结算冲突恢复回放结果留档失败 log=%s repair=%s',
                expedition_log_id,
                repair_id,
            )

    @classmethod
    def _replay_settlement(
        cls,
        expedition_log_id: int,
        party_id: int,
        repair_id: str,
        admin_user_id: int,
    ) -> dict:
        status = None
        replay_error = None
        try:
            from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
                progress_s1_expedition,
            )
            status, replay_error = progress_s1_expedition(party_id)
        except Exception as exc:  # noqa: BLE001
            replay_error = str(exc)

        current_log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(
            id=expedition_log_id,
        ).first()
        completed = bool(current_log is not None and current_log.status == 'completed')
        if completed:
            cls._record_replay_outcome(
                expedition_log_id,
                repair_id,
                admin_user_id=admin_user_id,
                completed=True,
                error_message=None,
            )
            return {
                'status': 'completed',
                'log_status': 'completed',
                'repair_id': repair_id,
                'replay_status': status or 'completed',
                'patch_applied': True,
                'can_restore': False,
            }

        error_text = cls._text(replay_error) or (
            f'结算重放未完成，runner 返回 {status or "unknown"}'
        )
        cls._record_replay_outcome(
            expedition_log_id,
            repair_id,
            admin_user_id=admin_user_id,
            completed=False,
            error_message=error_text,
        )
        return {
            'status': 'replay_failed',
            'log_status': current_log.status if current_log is not None else None,
            'repair_id': repair_id,
            'replay_status': status,
            'replay_error': error_text[:1000],
            'patch_applied': True,
            'can_restore': bool(
                current_log is not None and current_log.status == 'in_progress'
            ),
        }

    @classmethod
    def execute(
        cls,
        expedition_log_id: int,
        admin_user_id: int,
        preview_token: str,
    ):
        snapshot = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if snapshot is None:
            return None, '预览快照已过期，请重新预览后再执行'
        if (
            cls._integer(snapshot.get('version')) != cls.VERSION
            or cls._integer(snapshot.get('log_id')) != cls._integer(expedition_log_id)
        ):
            return None, '预览快照与当前日志不匹配'

        try:
            with db_manager.transaction():
                log = (
                    db_manager.query(AdventurerS1ExpeditionLog)
                    .filter_by(id=expedition_log_id)
                    .with_for_update()
                    .first()
                )
                if log is None:
                    raise ValueError('探险日志不存在')
                repair_id, party_id, already_completed = cls._apply_locked(
                    log, snapshot, int(admin_user_id),
                )
            if already_completed:
                return {
                    'status': 'completed',
                    'log_status': 'completed',
                    'repair_id': repair_id,
                    'patch_applied': True,
                    'idempotent': True,
                    'can_restore': False,
                }, None
            result = cls._replay_settlement(
                int(expedition_log_id), party_id, repair_id, int(admin_user_id),
            )
            if result.get('status') == 'completed':
                S1AdminScanSnapshotService.delete(
                    cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
                )
            return result, None
        except ValueError as exc:
            return None, str(exc)
        except DatabaseError as exc:
            if isinstance(exc.original_error, ValueError):
                return None, str(exc.original_error)
            current_app.logger.exception(
                'S1 探险结算冲突修复事务失败 log=%s', expedition_log_id,
            )
            return None, f'结算冲突修复执行失败，日志补丁已回滚：{exc}'
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 探险结算冲突修复执行失败 log=%s', expedition_log_id,
            )
            return None, f'结算冲突修复执行失败，日志补丁已回滚：{exc}'

    @classmethod
    def restore(
        cls,
        expedition_log_id: int,
        admin_user_id: int,
        preview_token: str,
    ):
        snapshot = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if snapshot is None:
            return None, '预览快照已过期，请重新预览后再回滚'
        if (
            cls._integer(snapshot.get('version')) != cls.VERSION
            or snapshot.get('mode') != 'retry'
            or cls._integer(snapshot.get('log_id')) != cls._integer(expedition_log_id)
        ):
            return None, '预览快照与当前日志不匹配'
        repair_id = cls._text(snapshot.get('repair_id'))
        if not repair_id:
            return None, '预览快照没有可回滚的修复批次'

        try:
            with db_manager.transaction():
                log = (
                    db_manager.query(AdventurerS1ExpeditionLog)
                    .filter_by(id=expedition_log_id)
                    .with_for_update()
                    .first()
                )
                if log is None:
                    raise ValueError('探险日志不存在')
                if log.status != 'in_progress':
                    raise ValueError(
                        f'日志状态为 {log.status}，结算已落地或状态已变化，不能回滚'
                    )
                _base, base_blockers = cls._base_context(log)
                if base_blockers:
                    raise ValueError(base_blockers[0]['message'])
                full_log = copy.deepcopy(log.full_log or {})
                marker = cls._find_repair(full_log, repair_id)
                if marker is None or marker.get('status') not in cls.ACTIVE_STATUSES:
                    raise ValueError('该修复批次当前不可回滚')
                if cls._semantic_log_hash(full_log) != marker.get('after_state_hash'):
                    raise ValueError('修复后的日志内容已变化，不能条件回滚')
                marker_blockers = cls._validate_marker_assets(
                    marker, lock_rows=True,
                )
                if marker_blockers:
                    raise ValueError(marker_blockers[0]['message'])

                updates, indexes, update_blockers = cls._member_update_maps(full_log)
                if update_blockers:
                    raise ValueError(update_blockers[0]['message'])
                for change in marker.get('member_changes') or []:
                    adventurer_id = cls._integer(change.get('adventurer_id'))
                    member_update = updates.get(adventurer_id)
                    if member_update is None:
                        raise ValueError(f'冒险者 #{adventurer_id} 的成员结算已缺失')
                    repair_kind = cls._text(change.get('repair_kind')) or (
                        'remove_conflicting_consumption'
                    )
                    if repair_kind == 'remove_conflicting_consumption':
                        if (
                            member_update.get('consumed_equipments')
                            != change.get('repaired_consumed_equipments')
                        ):
                            raise ValueError(
                                f'冒险者 #{adventurer_id} 的消耗字段已变化，不能回滚'
                            )
                        member_update['consumed_equipments'] = copy.deepcopy(
                            change.get('original_consumed_equipments')
                        )
                    elif repair_kind == 'remove_duplicate_new_after_stable_update':
                        original_traits_present = bool(
                            change.get('original_new_traits_present')
                        )
                        if (
                            member_update.get('new_equipments')
                            != change.get('repaired_new_equipments')
                            or (
                                original_traits_present
                                and member_update.get('new_traits')
                                != change.get('repaired_new_traits')
                            )
                        ):
                            raise ValueError(
                                f'冒险者 #{adventurer_id} 的同名新增或特质依赖字段'
                                '已变化，不能回滚'
                            )
                        member_update['new_equipments'] = copy.deepcopy(
                            change.get('original_new_equipments')
                        )
                        if original_traits_present:
                            member_update['new_traits'] = copy.deepcopy(
                                change.get('original_new_traits')
                            )
                        else:
                            member_update.pop('new_traits', None)
                    else:
                        raise ValueError(
                            f'未知的结算冲突修复类型：{repair_kind}'
                        )
                    full_log['member_updates'][indexes[adventurer_id]] = member_update

                cls._restore_progress_state(full_log, marker.get('progress_before') or {})
                if cls._semantic_log_hash(full_log) != marker.get('before_state_hash'):
                    raise ValueError('回滚后的日志哈希与修复前备份不一致')
                marker['status'] = 'restored'
                marker['restored_at'] = cls._now()
                marker['restored_by_user_id'] = int(admin_user_id)
                cls._replace_repair_entry(full_log, marker)
                log.full_log = full_log

            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            )
            return {
                'status': 'restored',
                'log_status': 'in_progress',
                'repair_id': repair_id,
                'restored_count': len(marker.get('candidates') or []),
            }, None
        except ValueError as exc:
            return None, str(exc)
        except DatabaseError as exc:
            if isinstance(exc.original_error, ValueError):
                return None, str(exc.original_error)
            current_app.logger.exception(
                'S1 探险结算冲突修复回滚事务失败 log=%s', expedition_log_id,
            )
            return None, f'结算冲突修复回滚失败，已整批回滚：{exc}'
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception(
                'S1 探险结算冲突修复回滚失败 log=%s', expedition_log_id,
            )
            return None, f'结算冲突修复回滚失败，已整批回滚：{exc}'
