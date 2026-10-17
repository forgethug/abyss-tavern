"""S1 同地牢公共地点现状：快照、并发写租约与版本历史。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta
from typing import Iterable

from flask import current_app
from sqlalchemy import case, or_

from db_repository import DatabaseError, db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog,
    AdventurerS1WorldLocationState,
    AdventurerS1WorldLocationStateHistory,
    AdventurerS1WorldOutlineDungeonState,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEON_NAMES,
    is_s1_independent_dungeon,
)


class LocationStateConflict(RuntimeError):
    pass


class S1WorldLocationStateService:
    @staticmethod
    def has_persistent_dungeon_identity(target_dungeon: str) -> bool:
        """常规配置副本保留稳定入口；生态终局只改变内部阶段。"""
        return str(target_dungeon or '').strip() in S1_ACTIVE_DUNGEON_NAMES

    @staticmethod
    def closed_dungeon_names(target_dungeons: Iterable[str] | None = None) -> set[str]:
        """批量返回可被硬关闭的非持久副本；常规配置副本始终保留入口。"""
        names = {
            str(value or '').strip()
            for value in (target_dungeons or [])
            if str(value or '').strip()
        }
        names.difference_update(S1_ACTIVE_DUNGEON_NAMES)
        query = AdventurerS1WorldOutlineDungeonState.query.filter(
            AdventurerS1WorldOutlineDungeonState.is_explorable.is_(False),
            AdventurerS1WorldOutlineDungeonState.target_dungeon.notin_(
                tuple(S1_ACTIVE_DUNGEON_NAMES),
            ),
        )
        if target_dungeons is not None:
            if not names:
                return set()
            query = query.filter(
                AdventurerS1WorldOutlineDungeonState.target_dungeon.in_(names),
            )
        rows = query.with_entities(
            AdventurerS1WorldOutlineDungeonState.target_dungeon,
        ).all()
        return {str(row[0]) for row in rows}

    @staticmethod
    def is_dungeon_dispatchable(target_dungeon: str) -> bool:
        """快速失败检查；发车事务仍必须调用锁内版本复查。"""
        dungeon = str(target_dungeon or '').strip()
        if not dungeon:
            return True
        if is_s1_independent_dungeon(dungeon):
            return True
        if S1WorldLocationStateService.has_persistent_dungeon_identity(dungeon):
            return True
        return dungeon not in S1WorldLocationStateService.closed_dungeon_names([dungeon])

    @staticmethod
    def lock_dungeon_dispatchable(target_dungeon: str) -> bool:
        """在调用方事务内锁住地牢聚合并复查发车条件。"""
        dungeon = str(target_dungeon or '').strip()
        if not dungeon:
            return True
        if is_s1_independent_dungeon(dungeon):
            return True
        if S1WorldLocationStateService.has_persistent_dungeon_identity(dungeon):
            return True
        state = (
            AdventurerS1WorldOutlineDungeonState.query
            .filter_by(target_dungeon=dungeon)
            .with_for_update()
            .populate_existing()
            .first()
        )
        return not state or state.is_explorable is not False

    @staticmethod
    def render_prompt_block(
        snapshot: dict,
        *,
        locus_prompt_block: str = '',
    ) -> str:
        """Render a white-listed authority snapshot; internal locus refs stay hidden."""
        if is_s1_independent_dungeon(snapshot.get('target_dungeon')):
            return ''
        from .s1_temporary_location_service import render_temporary_location_snapshot
        temporary_block = render_temporary_location_snapshot(snapshot)
        if temporary_block:
            return temporary_block
        dungeon = str(snapshot.get('target_dungeon') or '').strip()
        is_explorable = snapshot.get('is_explorable')
        identity_is_persistent = snapshot.get('identity_is_persistent') is True
        items = [
            {
                'location_id': item.get('location_id'),
                'location_name': str(item.get('location_name') or '').strip(),
                'revision': item.get('revision'),
                'current_state': str(item.get('current_state') or '').strip(),
                'public_risk_notice': str(item.get('public_risk_notice') or '').strip(),
            }
            for item in (snapshot.get('locations') or [])
            if isinstance(item, dict)
            and item.get('location_id') is not None
            and str(item.get('location_name') or '').strip()
        ]
        lines = []
        if str(locus_prompt_block or '').strip():
            lines.append(str(locus_prompt_block).strip())
        lines.extend([
            '【目标地牢公共地点权威现状】',
            '以下地点状态在本轮开始前已经成立并由各方共享；正文不得与其冲突。未列出的地点不代表不存在。',
            '整个地牢当前是否仍存在可正常进入的探索路径：'
            + (
                '是'
                if is_explorable is True
                else '否'
                if is_explorable is False
                else '未知（不得据此推断）'
            ),
        ])
        if identity_is_persistent:
            lines.append(
                '该目标是长期保留入口的常规地牢主题身份；旧地点、入口、资源关系或势力阶段可以终结，'
                '但必须在同名地牢内部承接历史形成当前可玩阶段，不得把整个地牢移出探索入口。'
            )
        for item in items:
            lines.append(
                f"- [location_id:{item['location_id']} revision:{item['revision']}] "
                f"{item['location_name']}：{item['current_state']}"
            )
            if item['public_risk_notice']:
                lines.append(f"  已传回酒馆的风险情报：{item['public_risk_notice']}")
        if not items:
            lines.append('- 当前尚无列入本次局部域的持久地点现状；这不表示目标地牢只有这一处或不存在其它区域。')
        return '\n'.join(lines)

    @staticmethod
    def build_snapshot(
        target_dungeon: str,
        *,
        preserve_persisted_explorability: bool = False,
        include_internal_locus_metadata: bool = False,
        preferred_location_ids: Iterable[int] | None = None,
    ) -> dict:
        if is_s1_independent_dungeon(target_dungeon):
            return {
                'target_dungeon': target_dungeon, 'locations': [],
                'is_explorable': True, 'prompt_block': '',
            }
        dungeon = str(target_dungeon or '').strip()
        identity_is_persistent = (
            S1WorldLocationStateService.has_persistent_dungeon_identity(dungeon)
        )
        limit = max(1, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_LOCATION_STATE_PROMPT_LIMIT', 20,
        ) or 20))
        dungeon_state = AdventurerS1WorldOutlineDungeonState.query.filter_by(
            target_dungeon=dungeon,
        ).first()
        persisted_is_explorable = (
            dungeon_state.is_explorable if dungeon_state else None
        )
        is_explorable = (
            persisted_is_explorable
            if preserve_persisted_explorability or not identity_is_persistent
            else True
        )
        selection_order = []
        if include_internal_locus_metadata:
            available = or_(
                AdventurerS1WorldLocationState.write_lease_log_id.is_(None),
                AdventurerS1WorldLocationState.write_lease_expires_at.is_(None),
                AdventurerS1WorldLocationState.write_lease_expires_at <= _portable_utc_now(),
            )
            selection_order.append(case((available, 0), else_=1))
            preferred_ids = set(preferred_location_ids or ())
            if preferred_ids:
                selection_order.append(case((
                    AdventurerS1WorldLocationState.id.in_(preferred_ids), 0,
                ), else_=1))
        rows = (
            AdventurerS1WorldLocationState.query
            .filter(
                AdventurerS1WorldLocationState.target_dungeon == dungeon,
                AdventurerS1WorldLocationState.current_state != '',
            )
            .order_by(
                *selection_order,
                AdventurerS1WorldLocationState.updated_at.desc(),
                AdventurerS1WorldLocationState.id.asc(),
            )
            .limit(limit)
            .all()
        )
        items = [{
            'location_id': row.id,
            'location_name': row.canonical_name,
            'revision': row.revision,
            'current_state': row.current_state,
            'public_risk_notice': row.public_risk_notice or '',
        } for row in rows]
        if include_internal_locus_metadata:
            now = _portable_utc_now()
            for item, row in zip(items, rows):
                item['state_updated_at'] = row.updated_at.isoformat() if row.updated_at else None
                item['_write_available'] = not bool(
                    row.write_lease_log_id is not None
                    and row.write_lease_expires_at
                    and row.write_lease_expires_at > now
                )
        lines = [
            '【目标地牢公共地点权威现状】',
            '以下地点状态在本轮开始前已经成立并由各方共享；正文不得与其冲突。未列出的地点不代表不存在。',
            '整个地牢当前是否仍存在可正常进入的探索路径：'
            + (
                '是'
                if is_explorable is True
                else '否'
                if is_explorable is False
                else '未知（不得据此推断）'
            ),
        ]
        if identity_is_persistent and not preserve_persisted_explorability:
            lines.append(
                '该目标是长期保留入口的常规地牢主题身份；旧地点、入口、资源关系或势力阶段可以终结，'
                '但必须在同名地牢内部承接历史形成当前可玩阶段，不得把整个地牢移出探索入口。'
            )
        for item in items:
            lines.append(
                f"- [location_id:{item['location_id']} revision:{item['revision']}] "
                f"{item['location_name']}：{item['current_state']}"
            )
            if item['public_risk_notice']:
                lines.append(f"  已传回酒馆的风险情报：{item['public_risk_notice']}")
        if not items:
            lines.append('- 当前尚无已登记的持久地点现状。')
        return {
            'target_dungeon': dungeon,
            'is_explorable': is_explorable,
            'identity_is_persistent': identity_is_persistent,
            'locations': items,
            'prompt_block': '\n'.join(lines),
            'captured_at': _portable_utc_now().isoformat(),
        }

    @staticmethod
    def include_proposed_location_states(
        snapshot: dict, changes: list[dict],
        *, refresh_existing: bool = True,
    ) -> dict:
        """补齐提案的精确现状；首次审核保留已有冻结状态，冲突复审才刷新。"""
        from db_repository.s1_world_location_state_repository import (
            list_proposed_location_states,
        )
        from .s1_temporary_location_service import (
            temporary_location_from_context, TEMPORARY_LOCATION_NAMESPACE,
        )
        destination = temporary_location_from_context(snapshot)
        if destination:
            changes = [row for row in changes if row.get('location_id') == destination['location_id']]

        location_ids: set[int] = set()
        location_names: set[str] = set()
        frozen_locations = snapshot.get('locations') or []
        frozen_ids = {item['location_id'] for item in frozen_locations}
        frozen_names = {item.get('location_name') for item in frozen_locations}
        for change in changes:
            location_id = change.get('location_id')
            if type(location_id) is int and location_id > 0:
                if refresh_existing or location_id not in frozen_ids:
                    location_ids.add(location_id)
            elif location_id is None:
                location_name = str(change.get('location_name') or '').strip()
                if location_name and (refresh_existing or location_name not in frozen_names):
                    location_names.add(location_name)
        if not (location_ids or location_names):
            return snapshot
        rows = list_proposed_location_states(
            TEMPORARY_LOCATION_NAMESPACE if destination else str(snapshot.get('target_dungeon') or '').strip(),
            location_ids=location_ids,
            location_names=location_names,
        )
        if not rows:
            return snapshot
        locations = {
            item['location_id']: dict(item)
            for item in snapshot.get('locations') or []
        }
        locations.update({
            item['location_id']: item for item in rows
            if refresh_existing or item['location_id'] not in frozen_ids
        })
        if list(locations.values()) == frozen_locations:
            return snapshot
        refreshed = {**snapshot, 'locations': list(locations.values())}
        refreshed['prompt_block'] = S1WorldLocationStateService.render_prompt_block(
            refreshed,
        )
        return refreshed

    @staticmethod
    def persist_snapshot(expedition_log_id: int | None, snapshot: dict) -> None:
        if not expedition_log_id:
            return
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().first()
            if not log or log.status != 'generating':
                return
            full_log = dict(log.full_log or {})
            full_log['location_state_snapshot'] = {
                'target_dungeon': snapshot.get('target_dungeon'),
                'is_explorable': snapshot.get('is_explorable'),
                'identity_is_persistent': snapshot.get('identity_is_persistent') is True,
                'locations': list(snapshot.get('locations') or []),
                'captured_at': snapshot.get('captured_at'),
            }
            from .s1_temporary_location_service import temporary_location_from_context, TEMPORARY_LOCATION_FIELD
            destination = temporary_location_from_context(full_log)
            if destination:
                full_log['location_state_snapshot'][TEMPORARY_LOCATION_FIELD] = destination
            log.full_log = full_log

    @staticmethod
    def load_snapshot(expedition_log_id: int) -> dict:
        log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).first()
        snapshot = (log.full_log or {}).get('location_state_snapshot') if log else None
        return dict(snapshot) if isinstance(snapshot, dict) else {}

    @staticmethod
    def claim_changes(
        expedition_log_id: int,
        target_dungeon: str,
        changes: list[dict],
        snapshot: dict,
    ) -> tuple[list[dict], list[str]]:
        if not changes:
            return [], []
        dungeon = str(target_dungeon or '').strip()
        if snapshot and str(snapshot.get('target_dungeon') or '').strip() != dungeon:
            return [], ['地点状态快照不属于本次目标地牢']
        now = _portable_utc_now()
        configured_lease_minutes = max(1, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_LOCATION_WRITE_LEASE_MINUTES', 15,
        ) or 15))
        progress_interval_minutes = max(1, int(current_app.config.get(
            'ADVENTURER_S1_EXPEDITION_PROGRESS_INTERVAL_MINUTES', 5,
        ) or 5))
        max_progress_pushes = max(1, int(current_app.config.get(
            'ADVENTURER_S1_LOG_MAX_PROGRESS_PUSHES', 8,
        ) or 8))
        lease_minutes = max(
            configured_lease_minutes,
            (max_progress_pushes + 1) * progress_interval_minutes,
        )
        try:
            with db_manager.transaction():
                from db_repository.s1_temporary_location_repository import location_namespace_for_log
                dungeon, temporary_id = location_namespace_for_log(expedition_log_id, dungeon)
                if temporary_id and any(row.get('location_id') != temporary_id for row in changes):
                    raise LocationStateConflict('临时地点只能更新本次冻结的地点身份')
                for change in changes:
                    if change.get('location_id') is not None:
                        continue
                    _, err = db_manager.execute_raw(
                        'INSERT INTO adventurer_s1_world_location_states '
                        '(target_dungeon, canonical_name, current_state, revision, created_at, updated_at) '
                        'VALUES (:dungeon, :name, \'\', 0, :now, :now) '
                        'ON CONFLICT (target_dungeon, canonical_name) DO NOTHING',
                        {'dungeon': dungeon, 'name': change['location_name'], 'now': now},
                    )
                    if err:
                        raise LocationStateConflict(f'创建地点占位失败：{err}')

                locked: list[tuple[dict, AdventurerS1WorldLocationState]] = []
                for change in sorted(
                    changes,
                    key=lambda item: (int(item.get('location_id') or 0), str(item.get('location_name') or '')),
                ):
                    if change.get('location_id') is not None:
                        row = AdventurerS1WorldLocationState.query.filter_by(
                            id=int(change['location_id']), target_dungeon=dungeon,
                        ).with_for_update().first()
                    else:
                        row = AdventurerS1WorldLocationState.query.filter_by(
                            target_dungeon=dungeon, canonical_name=change['location_name'],
                        ).with_for_update().first()
                    if not row:
                        raise LocationStateConflict('地点不存在或不属于本次目标地牢')
                    expected = int(change['expected_revision'])
                    if row.revision != expected:
                        raise LocationStateConflict(
                            f'地点「{row.canonical_name}」现状已更新至 revision {row.revision}'
                        )
                    if change.get('location_id') is None and row.current_state:
                        raise LocationStateConflict(
                            f'地点「{row.canonical_name}」已经存在：location_id={row.id}，'
                            f'revision={row.revision}，current_state={row.current_state}。'
                            '请从该现状继续推演并使用精确 ID/版本'
                        )
                    if (
                        row.write_lease_log_id not in (None, expedition_log_id)
                        and row.write_lease_expires_at
                        and row.write_lease_expires_at > now
                    ):
                        raise LocationStateConflict(f'地点「{row.canonical_name}」正由另一支队伍结算')
                    locked.append((change, row))

                enriched: list[dict] = []
                for change, row in locked:
                    row.write_lease_log_id = expedition_log_id
                    row.write_lease_expires_at = now + timedelta(minutes=lease_minutes)
                    item = dict(change)
                    item['location_id'] = row.id
                    item['location_name'] = row.canonical_name
                    enriched.append(item)
                return enriched, []
        except DatabaseError as exc:
            original = getattr(exc, 'original_error', None)
            if isinstance(original, LocationStateConflict):
                return [], [str(original)]
            raise

    @staticmethod
    def apply_claimed_changes(
        expedition_log_id: int,
        target_dungeon: str,
        changes: list[dict],
    ) -> list[dict]:
        """在调用方现有写回事务内提交；冲突时抛错并让整次写回回滚。"""
        if not changes:
            return []
        dungeon = str(target_dungeon or '').strip()
        ids = sorted({int(item['location_id']) for item in changes})
        from db_repository.s1_temporary_location_repository import location_namespace_for_log
        dungeon, temporary_id = location_namespace_for_log(expedition_log_id, dungeon)
        if temporary_id and ids != [temporary_id]:
            raise LocationStateConflict('临时地点只能更新本次冻结的地点身份')
        rows = (
            AdventurerS1WorldLocationState.query
            .filter(
                AdventurerS1WorldLocationState.target_dungeon == dungeon,
                AdventurerS1WorldLocationState.id.in_(ids),
            )
            .order_by(AdventurerS1WorldLocationState.id.asc())
            .with_for_update()
            .all()
        )
        row_by_id = {row.id: row for row in rows}
        for change in changes:
            row = row_by_id.get(int(change['location_id']))
            if not row or row.write_lease_log_id != expedition_log_id:
                raise LocationStateConflict('地点写租约已失效，请重新生成')
            if row.revision != int(change['expected_revision']):
                raise LocationStateConflict(f'地点「{row.canonical_name}」版本已变化，请重新生成')

        applied: list[dict] = []
        for change in changes:
            row = row_by_id[int(change['location_id'])]
            previous_state = row.current_state or ''
            previous_public_risk_notice = row.public_risk_notice or ''
            snapshot_revision = row.revision
            row.current_state = change['new_state']
            # Missing legacy notices mean unknown public information, not safety.
            row.public_risk_notice = change.get('public_risk_notice', '')
            row.revision += 1
            row.last_changed_log_id = expedition_log_id
            row.write_lease_log_id = None
            row.write_lease_expires_at = None
            history = AdventurerS1WorldLocationStateHistory(
                location_state_id=row.id,
                revision=row.revision,
                previous_state=previous_state,
                new_state=row.current_state,
                previous_public_risk_notice=previous_public_risk_notice,
                new_public_risk_notice=row.public_risk_notice,
                change_summary=change['change_summary'],
                source_log_id=expedition_log_id,
                snapshot_revision=snapshot_revision,
            )
            ok, err = db_manager.add(history, flush=False)
            if not ok:
                raise RuntimeError(f'地点状态历史写入失败：{err}')
            applied.append({
                'location_id': row.id,
                'location_name': row.canonical_name,
                'from_revision': snapshot_revision,
                'to_revision': row.revision,
                'new_state': row.current_state,
                'public_risk_notice': row.public_risk_notice,
                'change_summary': change['change_summary'],
            })
        return applied

    @staticmethod
    def claim_explorability_change(
        target_dungeon: str,
        change: dict | None,
        snapshot: dict,
        *,
        world_state_change_allowed: bool,
    ) -> tuple[dict | None, list[str]]:
        if not change:
            return None, []
        dungeon = str(target_dungeon or '').strip()
        if S1WorldLocationStateService.has_persistent_dungeon_identity(dungeon):
            # 常规副本整体入口是配置不变量。模型仍可推进地点、资源与世界线，
            # 但整体可探索性提案在消费前确定性丢弃，不触发昂贵的剧情重试。
            return None, []
        if snapshot and str(snapshot.get('target_dungeon') or '').strip() != dungeon:
            return None, ['地牢探索状态快照不属于本次目标地牢']
        if 'is_explorable' not in snapshot:
            return None, ['地牢探索状态冻结快照缺少 is_explorable']
        expected = change.get('expected_is_explorable')
        # null → bool 是把历史遗留的未知值初始化为本场已观察到的当前态，
        # 不构成宏观世界变化；已知 bool 之间的转换仍必须取得宏观写权限。
        if expected is not None and world_state_change_allowed is not True:
            return None, ['本轮未授权修改地牢整体可探索性']
        if expected != snapshot.get('is_explorable'):
            return None, ['地牢探索状态的 expected_is_explorable 与冻结快照不一致']
        with db_manager.transaction():
            row = AdventurerS1WorldOutlineDungeonState.query.filter_by(
                target_dungeon=dungeon,
            ).with_for_update().first()
            current = row.is_explorable if row else None
            if current != expected:
                return None, ['地牢探索状态已被另一支队伍更新，请重新生成']
        return dict(change), []

    @staticmethod
    def apply_claimed_explorability_change(
        target_dungeon: str,
        change: dict | None,
    ) -> dict | None:
        """在调用方现有写回事务内更新整个地牢的可探索状态。"""
        if not change:
            return None
        dungeon = str(target_dungeon or '').strip()
        if (
            S1WorldLocationStateService.has_persistent_dungeon_identity(dungeon)
            and change.get('is_explorable') is not True
        ):
            raise LocationStateConflict('常规副本身份不能被普通生态变化关闭')
        now = _portable_utc_now()
        row = AdventurerS1WorldOutlineDungeonState.query.filter_by(
            target_dungeon=dungeon,
        ).with_for_update().first()
        if not row:
            row = AdventurerS1WorldOutlineDungeonState(
                target_dungeon=dungeon,
                outline_revision=0,
                created_at=now,
                updated_at=now,
            )
            ok, err = db_manager.add(row, flush=True)
            if not ok:
                raise RuntimeError(f'创建地牢聚合状态失败：{err}')
        expected = change.get('expected_is_explorable')
        if row.is_explorable != expected:
            raise LocationStateConflict('地牢探索状态已变化，请重新生成')
        previous = row.is_explorable
        row.is_explorable = change['is_explorable']
        row.cycle_reseed_retry_after = None
        row.updated_at = now
        return {
            'target_dungeon': dungeon,
            'previous_is_explorable': previous,
            'is_explorable': row.is_explorable,
            'change_summary': change['change_summary'],
        }

    @staticmethod
    def apply_maintenance_location_changes(
        target_dungeon: str,
        changes: list[dict],
        *,
        snapshot: dict,
        expected_outline_revision: int,
    ) -> list[dict]:
        """Commit reviewed deltas and history in the existing maintenance transaction."""
        from plugins.adventurer_s1.services.s1_world_outline_contract import (
            normalize_maintenance_location_changes,
        )

        changes = normalize_maintenance_location_changes(changes, snapshot)
        if not changes:
            return []
        dungeon = str(target_dungeon or '').strip()
        state = AdventurerS1WorldOutlineDungeonState.query.filter_by(
            target_dungeon=dungeon,
        ).with_for_update().populate_existing().first()
        if (
            snapshot.get('target_dungeon') != dungeon
            or not state or state.is_explorable is not True
            or int(state.outline_revision or 0) != expected_outline_revision
        ):
            raise LocationStateConflict('维护地点所属地牢或版本已变化')
        frozen = {row['location_id']: row for row in snapshot['locations']}
        rows = AdventurerS1WorldLocationState.query.filter(
            AdventurerS1WorldLocationState.target_dungeon == dungeon,
            AdventurerS1WorldLocationState.id.in_(sorted(frozen)),
        ).order_by(AdventurerS1WorldLocationState.id.asc()).with_for_update(nowait=True).populate_existing().all()
        by_id = {row.id: row for row in rows}
        if set(by_id) != set(frozen) or any(
            row.revision != frozen[row.id]['revision']
            or row.canonical_name != frozen[row.id]['location_name']
            or row.current_state != frozen[row.id]['current_state']
            for row in rows
        ):
            raise LocationStateConflict('维护依据的地点现状已变化')
        now = _portable_utc_now()
        for change in changes:
            row = by_id[change['location_id']]
            if (
                row.write_lease_log_id is not None
                and row.write_lease_expires_at and row.write_lease_expires_at > now
            ):
                raise LocationStateConflict('维护地点正由探险结算持有写租约')
        applied = []
        for change in changes:
            row = by_id[change['location_id']]
            previous_state, revision = row.current_state, row.revision
            row.current_state = change['new_state']
            row.revision = revision + 1
            row.last_changed_log_id = None
            row.write_lease_log_id = None
            row.write_lease_expires_at = None
            row.updated_at = now
            # Background evolution cannot claim a newly witnessed public notice.
            notice = row.public_risk_notice or ''
            ok, err = db_manager.add(AdventurerS1WorldLocationStateHistory(
                location_state_id=row.id, revision=row.revision,
                previous_state=previous_state, new_state=row.current_state,
                previous_public_risk_notice=notice, new_public_risk_notice=notice,
                change_summary=change['change_summary'], source_log_id=None,
                snapshot_revision=revision,
            ), flush=False)
            if not ok:
                raise RuntimeError(f'维护地点状态历史写入失败：{err}')
            applied.append({
                'location_id': row.id, 'location_name': row.canonical_name,
                'from_revision': revision, 'to_revision': row.revision,
            })
        return applied

    @staticmethod
    def apply_cycle_reseed_transition(
        target_dungeon: str,
        transition: dict,
        *,
        expected_outline_revision: int,
    ) -> dict:
        """Apply one reviewed closed-dungeon successor inside the caller transaction."""
        from plugins.adventurer_s1.services.s1_world_location_state_contract import (
            normalize_dungeon_explorability_change,
            normalize_location_state_changes,
        )

        dungeon = str(target_dungeon or '').strip()
        raw_location_change = (
            transition.get('location_state_change')
            if isinstance(transition, dict)
            else None
        )
        location_changes, location_errors = normalize_location_state_changes(
            [raw_location_change],
            max_changes=1,
        )
        explorability_change, explorability_errors = (
            normalize_dungeon_explorability_change(
                transition.get('dungeon_explorability_change')
                if isinstance(transition, dict)
                else None,
            )
        )
        if location_errors or len(location_changes) != 1 or explorability_errors:
            raise LocationStateConflict(
                '循环播种地点状态合同已失效：'
                + '; '.join([
                    *location_errors,
                    *explorability_errors,
                ])[:800]
            )
        if (
            not explorability_change
            or explorability_change.get('expected_is_explorable') is not False
            or explorability_change.get('is_explorable') is not True
        ):
            raise LocationStateConflict('循环播种只能把已关闭地牢恢复为可探索')

        dungeon_state = AdventurerS1WorldOutlineDungeonState.query.filter_by(
            target_dungeon=dungeon,
        ).with_for_update().populate_existing().first()
        if (
            not dungeon_state
            or dungeon_state.is_explorable is not False
            or int(dungeon_state.outline_revision or 0) != int(expected_outline_revision)
        ):
            raise LocationStateConflict('循环播种地牢状态或版本已变化')

        change = location_changes[0]
        now = _portable_utc_now()
        if change.get('location_id') is None:
            _, err = db_manager.execute_raw(
                'INSERT INTO adventurer_s1_world_location_states '
                '(target_dungeon, canonical_name, current_state, revision, created_at, updated_at) '
                'VALUES (:dungeon, :name, \'\', 0, :now, :now) '
                'ON CONFLICT (target_dungeon, canonical_name) DO NOTHING',
                {
                    'dungeon': dungeon,
                    'name': change['location_name'],
                    'now': now,
                },
            )
            if err:
                raise LocationStateConflict(f'创建循环播种地点占位失败：{err}')
            location = AdventurerS1WorldLocationState.query.filter_by(
                target_dungeon=dungeon,
                canonical_name=change['location_name'],
            ).with_for_update().populate_existing().first()
        else:
            location = AdventurerS1WorldLocationState.query.filter_by(
                id=int(change['location_id']),
                target_dungeon=dungeon,
            ).with_for_update().populate_existing().first()
        if not location:
            raise LocationStateConflict('循环播种地点不存在或不属于目标地牢')
        if change.get('location_name') and location.canonical_name != change['location_name']:
            raise LocationStateConflict('循环播种地点名称与冻结身份不一致')
        if location.revision != int(change['expected_revision']):
            raise LocationStateConflict('循环播种地点 revision 已变化')
        if change.get('location_id') is None and location.current_state:
            raise LocationStateConflict('循环播种新地点已被其它任务建立')
        if (
            location.write_lease_log_id is not None
            and location.write_lease_expires_at
            and location.write_lease_expires_at > now
        ):
            raise LocationStateConflict('循环播种地点正由另一支队伍结算')

        previous_state = location.current_state or ''
        previous_public_risk_notice = location.public_risk_notice or ''
        snapshot_revision = int(location.revision or 0)
        location.current_state = change['new_state']
        # Offline reseeding has no witnessed publication authority.
        location.public_risk_notice = ''
        location.revision = snapshot_revision + 1
        location.last_changed_log_id = None
        location.write_lease_log_id = None
        location.write_lease_expires_at = None
        location.updated_at = now
        history = AdventurerS1WorldLocationStateHistory(
            location_state_id=location.id,
            revision=location.revision,
            previous_state=previous_state,
            new_state=location.current_state,
            previous_public_risk_notice=previous_public_risk_notice,
            new_public_risk_notice=location.public_risk_notice,
            change_summary=change['change_summary'],
            source_log_id=None,
            snapshot_revision=snapshot_revision,
        )
        ok, err = db_manager.add(history, flush=False)
        if not ok:
            raise RuntimeError(f'循环播种地点状态历史写入失败：{err}')

        dungeon_state.is_explorable = True
        dungeon_state.cycle_reseed_retry_after = None
        dungeon_state.updated_at = now
        return {
            'location_state_application': {
                'location_id': location.id,
                'location_name': location.canonical_name,
                'from_revision': snapshot_revision,
                'to_revision': location.revision,
                'new_state': location.current_state,
                'public_risk_notice': location.public_risk_notice,
                'change_summary': change['change_summary'],
            },
            'dungeon_explorability_application': {
                'target_dungeon': dungeon,
                'previous_is_explorable': False,
                'is_explorable': True,
                'change_summary': explorability_change['change_summary'],
            },
        }

    @staticmethod
    def _release_owned_location_leases(
        expedition_log_id: int,
        changes: list[dict],
    ) -> None:
        ids = sorted({
            int(item['location_id'])
            for item in changes
            if isinstance(item, dict) and item.get('location_id') is not None
        })
        if not ids:
            return
        rows = (
            AdventurerS1WorldLocationState.query
            .filter(AdventurerS1WorldLocationState.id.in_(ids))
            .order_by(AdventurerS1WorldLocationState.id.asc())
            .with_for_update()
            .populate_existing()
            .all()
        )
        for row in rows:
            if row.write_lease_log_id == expedition_log_id:
                row.write_lease_log_id = None
                row.write_lease_expires_at = None

    @staticmethod
    def apply_pending_changes_at_completion(
        expedition_log_id: int,
        target_dungeon: str,
        location_changes: list[dict],
        explorability_change: dict | None,
    ) -> dict:
        """在最终剧情批次已对玩家可见后落地；单项冲突留审计且不阻塞玩家结算。"""
        outcome = {
            'location_state_application': [],
            'dungeon_explorability_application': None,
            'errors': [],
        }
        if is_s1_independent_dungeon(target_dungeon):
            return outcome
        location_changes = list(location_changes or [])
        if location_changes:
            savepoint = db_manager.begin_nested()
            try:
                outcome['location_state_application'] = (
                    S1WorldLocationStateService.apply_claimed_changes(
                        expedition_log_id,
                        target_dungeon,
                        location_changes,
                    )
                )
                savepoint.commit()
            except Exception as exc:
                if savepoint.is_active:
                    savepoint.rollback()
                outcome['errors'].append(
                    f'location_state_application_failed:{type(exc).__name__}:{str(exc)[:500]}'
                )
                try:
                    S1WorldLocationStateService._release_owned_location_leases(
                        expedition_log_id,
                        location_changes,
                    )
                except Exception as release_exc:
                    outcome['errors'].append(
                        'location_state_lease_release_failed:'
                        f'{type(release_exc).__name__}:{str(release_exc)[:500]}'
                    )

        if explorability_change:
            savepoint = db_manager.begin_nested()
            try:
                outcome['dungeon_explorability_application'] = (
                    S1WorldLocationStateService.apply_claimed_explorability_change(
                        target_dungeon,
                        explorability_change,
                    )
                )
                savepoint.commit()
            except Exception as exc:
                if savepoint.is_active:
                    savepoint.rollback()
                outcome['errors'].append(
                    'dungeon_explorability_application_failed:'
                    f'{type(exc).__name__}:{str(exc)[:500]}'
                )
        return outcome
