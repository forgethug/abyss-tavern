"""Repair old fate settlements that revoked rewards but still deleted replaced abilities."""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime

from flask import current_app
from sqlalchemy import String, cast, func, or_

from db_repository import db_manager
from plugins.adventurer.services.adventurer_parser_service import (
    _normalize_new_skills_payload,
    _normalize_removed_name_list_payload,
)
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1FateJourney,
    AdventurerS1Settings,
    AdventurerS1Skill,
    AdventurerS1Trait,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_fate_destructive_change_contract import (
    FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD,
    FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD,
)
from plugins.adventurer_s1.services.s1_fate_historical_review_contract import (
    SCHEMA_VERSION as HISTORICAL_REVIEW_SCHEMA_VERSION,
    canonical_digest,
    stable_ref,
    validate_historical_review,
)


class S1FateSettlementRepairService:
    """Restore only deletions mechanically attributable to confirmed fate incidents."""

    # v4 admits only reviewed historical candidates carrying a frozen evidence
    # digest; all earlier preview tokens remain invalid.
    SNAPSHOT_OPERATION = 'fate_settlement_repair_v4'
    EVIDENCE_OPERATION = 'fate_historical_review_evidence_v1'
    BACKUP_KEY = 'fate_settlement_repair_backup'
    # 命途系统上线时间。更早的冷日志不可能包含本事故标记，不应为一次管理员
    # 预览解压并传输全量历史 full_log。
    AFFECTED_FROM = datetime(2026, 7, 12)
    REJECTED_MARKER = '"_fate_rejected_reward_manifest"'
    CONFIRMED_UNMARKED_TRAIT_DELETIONS = {
        11318: {
            49: frozenset({'十二殉道者魂魄印记'}),
        },
    }

    @staticmethod
    def _journey_retry_state(journey: AdventurerS1FateJourney) -> dict:
        return {
            'status': str(journey.status or ''),
            'breakthrough_completed': bool(journey.breakthrough_completed),
            'final_outcome': journey.final_outcome,
            'final_expedition_log_id': (
                int(journey.final_expedition_log_id)
                if journey.final_expedition_log_id is not None
                else None
            ),
            'completed_at': (
                journey.completed_at.isoformat()
                if journey.completed_at
                else None
            ),
        }

    @staticmethod
    def _apply_journey_retry_state(
        journey: AdventurerS1FateJourney,
        state: dict,
    ) -> None:
        journey.status = str(state.get('status') or '')
        journey.breakthrough_completed = bool(
            state.get('breakthrough_completed'),
        )
        journey.final_outcome = state.get('final_outcome')
        journey.final_expedition_log_id = state.get('final_expedition_log_id')
        completed_at = state.get('completed_at')
        journey.completed_at = (
            datetime.fromisoformat(str(completed_at).removesuffix('Z'))
            if completed_at
            else None
        )

    @staticmethod
    def _opened_final_retry_state() -> dict:
        return {
            'status': 'final_ready',
            'breakthrough_completed': False,
            'final_outcome': None,
            'final_expedition_log_id': None,
            'completed_at': None,
        }

    @staticmethod
    def _invalidate_adventurer_details(adventurer_ids: set[int]) -> None:
        if not adventurer_ids:
            return
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
        )
        for adventurer_id in sorted(adventurer_ids):
            invalidate_adventurer_detail_cache(adventurer_id)

    @staticmethod
    def _created_state(row: object, resource_type: str) -> dict:
        """Return the mutable state created by this repair, excluding audit timestamps."""
        if resource_type == 'skill':
            return {
                'description': str(getattr(row, 'description', '') or ''),
                'equipped': bool(getattr(row, 'equipped', False)),
            }
        return {
            'description': str(getattr(row, 'description', '') or ''),
            'description_ai': getattr(row, 'description_ai', None),
            'description_ai_at': (
                row.description_ai_at.isoformat()
                if getattr(row, 'description_ai_at', None)
                else None
            ),
            'description_ai_source_hash': getattr(
                row,
                'description_ai_source_hash',
                None,
            ),
            'lore_entries': getattr(row, 'lore_entries', None),
            'is_archived': bool(getattr(row, 'is_archived', False)),
            'is_merged_trait': bool(getattr(row, 'is_merged_trait', False)),
            'merged_from': getattr(row, 'merged_from', None),
            'merged_into': getattr(row, 'merged_into', None),
            'merge_generated_by': getattr(row, 'merge_generated_by', None),
            'recovery_policy': str(
                getattr(row, 'recovery_policy', 'none') or 'none',
            ),
            'recovery_stage': getattr(row, 'recovery_stage', None),
            'recovery_progress': int(
                getattr(row, 'recovery_progress', 0) or 0,
            ),
            'recovery_required': getattr(row, 'recovery_required', None),
            'recovery_hours': getattr(row, 'recovery_hours', None),
            'recovery_expires_at': (
                row.recovery_expires_at.isoformat()
                if getattr(row, 'recovery_expires_at', None)
                else None
            ),
            'recovery_rest_baseline_seconds': getattr(
                row,
                'recovery_rest_baseline_seconds',
                None,
            ),
            'recovered_desc': getattr(row, 'recovered_desc', None),
            'archived_reason': getattr(row, 'archived_reason', None),
            'archived_at': (
                row.archived_at.isoformat()
                if getattr(row, 'archived_at', None)
                else None
            ),
        }

    @classmethod
    def _legacy_created_state(cls, item: dict) -> dict:
        """Reconstruct the exact state produced before created_state was recorded."""
        if item.get('resource_type') == 'skill':
            return {
                'description': str(item.get('description') or ''),
                'equipped': True,
            }
        metadata = item.get('metadata') or {}
        return {
            'description': str(item.get('description') or ''),
            'description_ai': None,
            'description_ai_at': None,
            'description_ai_source_hash': None,
            'lore_entries': None,
            'is_archived': False,
            'is_merged_trait': False,
            'merged_from': None,
            'merged_into': None,
            'merge_generated_by': None,
            'recovery_policy': str(
                metadata.get('recovery_policy') or 'none',
            ),
            'recovery_stage': metadata.get('recovery_stage'),
            'recovery_progress': int(
                metadata.get('recovery_progress') or 0,
            ),
            'recovery_required': metadata.get('recovery_required'),
            'recovery_hours': None,
            'recovery_expires_at': None,
            'recovery_rest_baseline_seconds': None,
            'recovered_desc': None,
            'archived_reason': None,
            'archived_at': None,
        }

    @staticmethod
    def _named_entries(value: object) -> list[dict]:
        entries = []
        if isinstance(value, dict):
            rows = value.items()
        elif isinstance(value, list):
            rows = enumerate(value)
        else:
            rows = []
        for key, raw in rows:
            if isinstance(raw, dict):
                name = str(raw.get('name') or (key if not isinstance(key, int) else '')).strip()
                description = str(raw.get('desc') or raw.get('description') or '').strip()
                metadata = dict(raw)
            else:
                name = str(key if isinstance(value, dict) else raw or '').strip()
                description = str(raw or '').strip() if isinstance(value, dict) else ''
                metadata = {}
            if name:
                entries.append({
                    'name': name,
                    'description': description,
                    'metadata': metadata,
                })
        return entries

    @staticmethod
    def _snapshot_restore_identity(item: dict, resource_type: str) -> dict:
        """Normalize only fields that the repair would actually restore."""
        identity = {
            'description': str(item.get('description') or ''),
        }
        if resource_type != 'trait':
            return identity
        metadata = (
            item.get('metadata')
            if isinstance(item.get('metadata'), dict)
            else {}
        )
        identity['metadata'] = {
            'recovery_policy': str(
                metadata.get('recovery_policy') or 'none',
            ),
            'recovery_stage': metadata.get('recovery_stage'),
            'recovery_progress': int(
                metadata.get('recovery_progress') or 0,
            ),
            'recovery_required': metadata.get('recovery_required'),
        }
        return identity

    @staticmethod
    def _member_update(full_log: dict, adventurer_id: int) -> dict:
        for row in full_log.get('member_updates') or []:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get('id') or 0) == adventurer_id:
                    return row
            except (TypeError, ValueError):
                continue
        return {}

    @staticmethod
    def _member_snapshot(full_log: dict, adventurer_id: int) -> dict:
        for row in full_log.get('audit_member_snapshot') or []:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get('adventurer_id') or 0) == adventurer_id:
                    return row
            except (TypeError, ValueError):
                continue
        return {}

    @classmethod
    def _incident_rows(
        cls,
        *,
        expedition_log_id: int | None = None,
    ) -> list[tuple[int, object]]:
        """只取带事故标记的热日志。

        上线修复前已对当时全部 8653 条 completed 日志做过一次完整解压扫描，
        确认 8 条事故标记均仍是热日志。未来路径已有发布前原子保护，因此这里
        不再为每次管理员预览反复解压已排除的 5934 条冷日志。
        """
        full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
        query = AdventurerS1ExpeditionLog.query.with_entities(
            AdventurerS1ExpeditionLog.id,
            AdventurerS1ExpeditionLog.full_log,
        ).filter(
            AdventurerS1ExpeditionLog.status == 'completed',
            AdventurerS1ExpeditionLog.created_at >= cls.AFFECTED_FROM,
            AdventurerS1ExpeditionLog.cold_compressed_at.is_(None),
            full_log_text.contains(cls.REJECTED_MARKER),
        )
        if expedition_log_id:
            query = query.filter(AdventurerS1ExpeditionLog.id == int(expedition_log_id))
        return query.order_by(AdventurerS1ExpeditionLog.id.asc()).all()

    @classmethod
    def _confirmed_unmarked_rows(
        cls,
        *,
        expedition_log_id: int | None = None,
    ) -> list[tuple[int, object]]:
        """只在管理员定向预览时读取已确认、无旧事故标记的精确日志。"""
        if expedition_log_id is None:
            return []
        requested_id = int(expedition_log_id)
        if requested_id not in cls.CONFIRMED_UNMARKED_TRAIT_DELETIONS:
            return []
        return (
            AdventurerS1ExpeditionLog.query.with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(
                AdventurerS1ExpeditionLog.id == requested_id,
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.cold_compressed_at.is_(None),
            )
            .all()
        )

    @staticmethod
    def _has_final_fate_destructive_receipt(full_log: dict) -> bool:
        context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
        resolution = full_log.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD)
        settlement_receipt = full_log.get('settlement_receipt')
        id_receipt = (
            settlement_receipt.get('final_fate_destructive_ids')
            if isinstance(settlement_receipt, dict)
            else None
        )
        if not all(isinstance(row, dict) for row in (
            context,
            resolution,
            id_receipt,
        )):
            return False
        context_id = str(context.get('context_id') or '').strip()
        return bool(
            context_id
            and resolution.get('context_id') == context_id
            and id_receipt.get('context_id') == context_id
            and id_receipt.get('status') == 'passed'
        )

    @classmethod
    def _unreviewed_final_fate_rows(
        cls,
        *,
        expedition_log_id: int | None = None,
    ) -> list[tuple[int, object]]:
        """Load logs that can contain a legacy final-fate removal.

        Hot JSON can be filtered by field name in SQL.  Cold logs are stored as
        compressed envelopes, so the ORM must load and transparently decompress
        them before the structural checks below can inspect their contents.
        """
        full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
        query = AdventurerS1ExpeditionLog.query.with_entities(
            AdventurerS1ExpeditionLog.id,
            AdventurerS1ExpeditionLog.full_log,
        ).filter(
            AdventurerS1ExpeditionLog.status == 'completed',
            AdventurerS1ExpeditionLog.created_at >= cls.AFFECTED_FROM,
        )
        if expedition_log_id is not None:
            query = query.filter(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
            )
        else:
            query = query.filter(or_(
                AdventurerS1ExpeditionLog.cold_compressed_at.isnot(None),
                full_log_text.contains('"removed_traits"'),
                full_log_text.contains('"removed_skills"'),
            ))
        return query.order_by(AdventurerS1ExpeditionLog.id.asc()).all()

    @classmethod
    def _scan_unreviewed_final_fate_destructive_changes(
        cls,
        rows: list[tuple[int, object]],
        *,
        excluded_log_ids: set[int],
    ) -> dict:
        """Report legacy destructive proposals without making them executable."""
        reports = []
        for log_id, raw_full_log in rows:
            log_id = int(log_id)
            if log_id in excluded_log_ids:
                continue
            full_log = raw_full_log if isinstance(raw_full_log, dict) else {}
            fate_context = full_log.get('fate_journey_context')
            if (
                not isinstance(fate_context, dict)
                or fate_context.get('event_type') != 'final'
                or cls._has_final_fate_destructive_receipt(full_log)
            ):
                continue

            focus_state = (
                fate_context.get('character_evidence', {}).get(
                    'current_state',
                    {},
                )
                if isinstance(fate_context.get('character_evidence'), dict)
                else {}
            )
            if not isinstance(focus_state, dict):
                focus_state = {}
            try:
                focus_adventurer_id = int(
                    fate_context.get('adventurer_id') or 0,
                )
            except (TypeError, ValueError):
                focus_adventurer_id = 0

            for update in full_log.get('member_updates') or []:
                if not isinstance(update, dict):
                    continue
                try:
                    adventurer_id = int(update.get('id') or 0)
                except (TypeError, ValueError):
                    continue
                if adventurer_id <= 0:
                    continue
                removed_traits = sorted(set(
                    _normalize_removed_name_list_payload(
                        update.get('removed_traits'),
                    ),
                ))
                removed_skills = sorted(set(
                    _normalize_removed_name_list_payload(
                        update.get('removed_skills'),
                    ),
                ))
                if not removed_traits and not removed_skills:
                    continue

                snapshot = cls._member_snapshot(full_log, adventurer_id)
                character_state = (
                    focus_state
                    if adventurer_id == focus_adventurer_id
                    else {}
                )
                frozen_traits = {
                    item['name']
                    for item in [
                        *cls._named_entries(character_state.get('traits')),
                        *cls._named_entries(snapshot.get('traits')),
                    ]
                }
                frozen_skills = {
                    item['name']
                    for item in [
                        *cls._named_entries(character_state.get('skills')),
                        *cls._named_entries(snapshot.get('skills')),
                    ]
                }
                new_traits = {
                    item['name']
                    for item in cls._named_entries(update.get('new_traits'))
                }
                new_skills = {
                    str(item.get('name') or '').strip()
                    for item in _normalize_new_skills_payload(
                        update.get('new_skills'),
                        split_semantic_name_desc=False,
                    )
                    if str(item.get('name') or '').strip()
                }
                reports.append({
                    'expedition_log_id': log_id,
                    'adventurer_id': adventurer_id,
                    'adventurer_name': str(
                        snapshot.get('adventurer_name') or '',
                    ).strip(),
                    'removed_traits': removed_traits,
                    'removed_skills': removed_skills,
                    'snapshot_missing_traits': [
                        name for name in removed_traits
                        if name not in frozen_traits
                    ],
                    'snapshot_missing_skills': [
                        name for name in removed_skills
                        if name not in frozen_skills
                    ],
                    'same_log_changed_traits': [
                        name for name in removed_traits
                        if name in new_traits
                    ],
                    'same_log_changed_skills': [
                        name for name in removed_skills
                        if name in new_skills
                    ],
                })

        adventurer_ids = sorted({
            int(item['adventurer_id']) for item in reports
        })
        trait_names = sorted({
            name
            for item in reports
            for name in item['removed_traits']
        })
        skill_names = sorted({
            name
            for item in reports
            for name in item['removed_skills']
        })
        existing_traits = set()
        existing_skills = set()
        if adventurer_ids and trait_names:
            existing_traits = {
                (int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Trait.query.filter(
                    AdventurerS1Trait.adventurer_id.in_(adventurer_ids),
                    AdventurerS1Trait.name.in_(trait_names),
                ).all()
            }
        if adventurer_ids and skill_names:
            existing_skills = {
                (int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Skill.query.filter(
                    AdventurerS1Skill.adventurer_id.in_(adventurer_ids),
                    AdventurerS1Skill.name.in_(skill_names),
                ).all()
            }
        adventurer_names = dict(
            AdventurerS1.query.with_entities(
                AdventurerS1.id,
                AdventurerS1.name,
            ).filter(AdventurerS1.id.in_(adventurer_ids)).all()
        ) if adventurer_ids else {}
        for item in reports:
            adventurer_id = int(item['adventurer_id'])
            item['adventurer_name'] = (
                adventurer_names.get(adventurer_id)
                or item['adventurer_name']
                or f'#{adventurer_id}'
            )
            item['currently_missing_traits'] = [
                name for name in item['removed_traits']
                if (adventurer_id, name) not in existing_traits
            ]
            item['currently_missing_skills'] = [
                name for name in item['removed_skills']
                if (adventurer_id, name) not in existing_skills
            ]
        reports.sort(key=lambda item: (
            int(item['expedition_log_id']),
            int(item['adventurer_id']),
        ))
        return {
            'unreviewed_scan_logs': len(rows),
            'unreviewed_destructive_log_count': len({
                int(item['expedition_log_id']) for item in reports
            }),
            'unreviewed_destructive_member_count': len(reports),
            'unreviewed_destructive_item_count': sum(
                len(item['removed_traits']) + len(item['removed_skills'])
                for item in reports
            ),
            'unreviewed_current_missing_count': sum(
                len(item['currently_missing_traits'])
                + len(item['currently_missing_skills'])
                for item in reports
            ),
            'unreviewed_snapshot_missing_count': sum(
                len(item['snapshot_missing_traits'])
                + len(item['snapshot_missing_skills'])
                for item in reports
            ),
            'unreviewed_destructive_logs': reports,
        }

    @staticmethod
    def _historical_review_events(full_log: dict) -> list[dict]:
        """Project complete narrative sections to stable, reviewable event rows."""
        rows: list[dict] = []
        raw_events = full_log.get('events')
        if isinstance(raw_events, list):
            event_values = raw_events
        elif isinstance(raw_events, dict):
            event_values = [
                {'source_key': str(key), 'content': value}
                for key, value in raw_events.items()
            ]
        elif raw_events in (None, ''):
            event_values = []
        else:
            event_values = [raw_events]
        for value in event_values:
            content = (
                value.strip()
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
            if content:
                rows.append({
                    'event_index': len(rows) + 1,
                    'source': 'events',
                    'content': content,
                })

        for field in (
            'story',
            'summary',
            'adventure_summary',
            'final_summary',
            'adventure_history',
        ):
            value = full_log.get(field)
            if value in (None, '', [], {}):
                continue
            content = (
                value.strip()
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
            if content:
                rows.append({
                    'event_index': len(rows) + 1,
                    'source': field,
                    'content': content,
                })
        if len(rows) > 512:
            raise ValueError('剧情事件超过单次历史删除复核上限 512 条')
        if sum(len(item['content']) for item in rows) > 300_000:
            raise ValueError('剧情证据超过单次历史删除复核上限 300000 字符')
        return rows

    @classmethod
    def _build_historical_review_evidence(
        cls,
        *,
        expedition_log_id: int,
    ) -> dict:
        log = AdventurerS1ExpeditionLog.query.filter(
            AdventurerS1ExpeditionLog.id == int(expedition_log_id),
            AdventurerS1ExpeditionLog.status == 'completed',
        ).first()
        if log is None:
            raise ValueError('没有找到已完成的探险日志')
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        fate_context = full_log.get('fate_journey_context')
        if (
            not isinstance(fate_context, dict)
            or fate_context.get('event_type') != 'final'
        ):
            raise ValueError('该日志不是最终命途结算')
        if cls._has_final_fate_destructive_receipt(full_log):
            raise ValueError('该日志已有完整逐项删除审核回执，不属于历史未审核删除')

        try:
            focus_adventurer_id = int(fate_context.get('adventurer_id') or 0)
        except (TypeError, ValueError):
            focus_adventurer_id = 0
        focus_state = (
            fate_context.get('character_evidence', {}).get('current_state', {})
            if isinstance(fate_context.get('character_evidence'), dict)
            else {}
        )
        if not isinstance(focus_state, dict):
            focus_state = {}

        raw_actions: list[dict] = []
        successor_rows: list[dict] = []
        adventurer_ids: set[int] = set()
        for update in full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                adventurer_id = int(update.get('id') or 0)
            except (TypeError, ValueError):
                continue
            if adventurer_id <= 0:
                continue
            removed_by_type = {
                'trait': sorted(set(_normalize_removed_name_list_payload(
                    update.get('removed_traits'),
                ))),
                'skill': sorted(set(_normalize_removed_name_list_payload(
                    update.get('removed_skills'),
                ))),
            }
            if not any(removed_by_type.values()):
                continue
            adventurer_ids.add(adventurer_id)
            snapshot = cls._member_snapshot(full_log, adventurer_id)
            snapshot_sources = {
                'trait': cls._named_entries(snapshot.get('traits')),
                'skill': cls._named_entries(snapshot.get('skills')),
            }
            fallback_snapshot_sources = {'trait': [], 'skill': []}
            if adventurer_id == focus_adventurer_id:
                fallback_snapshot_sources = {
                    'trait': cls._named_entries(focus_state.get('traits')),
                    'skill': cls._named_entries(focus_state.get('skills')),
                }

            new_by_type = {
                'trait': cls._named_entries(update.get('new_traits')),
                'skill': [
                    {
                        'name': str(item.get('name') or '').strip(),
                        'description': str(item.get('description') or '').strip(),
                        'metadata': dict(item),
                    }
                    for item in _normalize_new_skills_payload(
                        update.get('new_skills'),
                        split_semantic_name_desc=False,
                    )
                    if str(item.get('name') or '').strip()
                ],
            }
            local_successor_refs: dict[str, list[str]] = {'trait': [], 'skill': []}
            for resource_type, new_rows in new_by_type.items():
                for ordinal, item in enumerate(new_rows, start=1):
                    successor_ref = stable_ref(
                        'suc',
                        expedition_log_id,
                        adventurer_id,
                        resource_type,
                        item['name'],
                        ordinal,
                    )
                    local_successor_refs[resource_type].append(successor_ref)
                    successor_rows.append({
                        'successor_ref': successor_ref,
                        'adventurer_id': adventurer_id,
                        'resource_type': resource_type,
                        'name': item['name'],
                        'description': item.get('description') or '',
                    })

            for resource_type, removed_names in removed_by_type.items():
                for name in removed_names:
                    authoritative_matches = [
                        item for item in snapshot_sources[resource_type]
                        if item['name'] == name
                    ]
                    matches = authoritative_matches or [
                        item for item in fallback_snapshot_sources[resource_type]
                        if item['name'] == name
                    ]
                    snapshots = {
                        canonical_digest(cls._snapshot_restore_identity(
                            item,
                            resource_type,
                        )): item
                        for item in matches
                    }
                    frozen = next(iter(snapshots.values()), None)
                    action_ref = stable_ref(
                        'act',
                        expedition_log_id,
                        adventurer_id,
                        resource_type,
                        name,
                    )
                    raw_actions.append({
                        'action_ref': action_ref,
                        'adventurer_id': adventurer_id,
                        'resource_type': resource_type,
                        'name': name,
                        'description': (
                            str(frozen.get('description') or '')
                            if frozen
                            else ''
                        ),
                        'metadata': {
                            key: frozen.get('metadata', {}).get(key)
                            for key in (
                                'recovery_policy',
                                'recovery_stage',
                                'recovery_progress',
                                'recovery_required',
                            )
                            if frozen and key in frozen.get('metadata', {})
                        },
                        'snapshot_available': frozen is not None,
                        'snapshot_conflict': len(snapshots) > 1,
                        'same_log_changed': any(
                            item['name'] == name for item in new_by_type[resource_type]
                        ),
                        'same_log_successor_refs': list(
                            local_successor_refs[resource_type],
                        ),
                        'currently_missing': False,
                        'later_structured_touches': [],
                    })

        if not raw_actions:
            raise ValueError('该日志没有可复核的旧特质或旧技能删除项')
        if len(raw_actions) > 256:
            raise ValueError('删除项超过单次历史复核上限 256 项')

        adventurer_names = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(sorted(adventurer_ids))).all()
        ) if adventurer_ids else {}
        trait_names = sorted({
            item['name'] for item in raw_actions
            if item['resource_type'] == 'trait'
        })
        skill_names = sorted({
            item['name'] for item in raw_actions
            if item['resource_type'] == 'skill'
        })
        existing: set[tuple[str, int, str]] = set()
        if adventurer_ids and trait_names:
            existing.update(
                ('trait', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Trait.query.filter(
                    AdventurerS1Trait.adventurer_id.in_(sorted(adventurer_ids)),
                    AdventurerS1Trait.name.in_(trait_names),
                ).all()
            )
        if adventurer_ids and skill_names:
            existing.update(
                ('skill', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Skill.query.filter(
                    AdventurerS1Skill.adventurer_id.in_(sorted(adventurer_ids)),
                    AdventurerS1Skill.name.in_(skill_names),
                ).all()
            )

        action_by_key = {
            (item['resource_type'], int(item['adventurer_id']), item['name']): item
            for item in raw_actions
        }
        relevant_rows = cls._relevant_rows(
            adventurer_ids=adventurer_ids,
            min_log_id=int(expedition_log_id) + 1,
        )
        for later_log_id, raw_later_log in relevant_rows:
            if int(later_log_id) <= int(expedition_log_id):
                continue
            later_log = raw_later_log if isinstance(raw_later_log, dict) else {}
            for key, action in action_by_key.items():
                update = cls._member_update(later_log, int(action['adventurer_id']))
                if not update:
                    continue
                if action['resource_type'] == 'trait':
                    removed = set(_normalize_removed_name_list_payload(
                        update.get('removed_traits'),
                    ))
                    added = {
                        item['name']
                        for item in cls._named_entries(update.get('new_traits'))
                    }
                else:
                    removed = set(_normalize_removed_name_list_payload(
                        update.get('removed_skills'),
                    ))
                    added = {
                        str(item.get('name') or '').strip()
                        for item in _normalize_new_skills_payload(
                            update.get('new_skills'),
                            split_semantic_name_desc=False,
                        )
                    }
                if action['name'] in removed or action['name'] in added:
                    action['later_structured_touches'].append({
                        'expedition_log_id': int(later_log_id),
                        'removed': action['name'] in removed,
                        'added': action['name'] in added,
                    })

        for item in raw_actions:
            key = (
                item['resource_type'],
                int(item['adventurer_id']),
                item['name'],
            )
            item['adventurer_name'] = (
                adventurer_names.get(int(item['adventurer_id']))
                or f"#{item['adventurer_id']}"
            )
            item['currently_missing'] = key not in existing

        evidence = {
            'schema_version': HISTORICAL_REVIEW_SCHEMA_VERSION,
            'expedition_log_id': int(log.id),
            'party_name': str(log.party_name or ''),
            'target_dungeon': str(log.target_dungeon or ''),
            'scan_max_log_id': int(db_manager.query(
                func.max(AdventurerS1ExpeditionLog.id),
            ).filter(AdventurerS1ExpeditionLog.status == 'completed').scalar() or 0),
            'events': cls._historical_review_events(full_log),
            'removed_assets': raw_actions,
            'same_log_successors': successor_rows,
        }
        evidence['evidence_digest'] = canonical_digest(evidence)
        return evidence

    @classmethod
    def load_historical_review_evidence(
        cls,
        admin_user_id: int,
        *,
        expedition_log_id: int,
    ):
        try:
            evidence = cls._build_historical_review_evidence(
                expedition_log_id=int(expedition_log_id),
            )
            evidence_token = S1AdminScanSnapshotService.store(
                cls.EVIDENCE_OPERATION,
                admin_user_id,
                {
                    'expedition_log_id': int(evidence['expedition_log_id']),
                    'scan_max_log_id': int(evidence['scan_max_log_id']),
                    'evidence_digest': str(evidence['evidence_digest']),
                },
                allow_signed_fallback=True,
            )
            if not evidence_token:
                return None, '无法创建历史删除证据快照'
            return {
                'evidence_token': evidence_token,
                'evidence': evidence,
            }, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 历史命途删除证据读取失败')
            return None, f'读取复核证据失败：{exc}'

    @classmethod
    def _load_historical_review_evidence_snapshot(
        cls,
        admin_user_id: int,
        evidence_token: str,
    ) -> tuple[dict | None, str | None]:
        pointer = S1AdminScanSnapshotService.load(
            cls.EVIDENCE_OPERATION,
            admin_user_id,
            evidence_token,
            allow_signed_fallback=True,
        )
        if pointer is None:
            return None, '复核证据快照已过期，请重新读取'
        try:
            evidence = cls._build_historical_review_evidence(
                expedition_log_id=int(pointer.get('expedition_log_id') or 0),
            )
        except (TypeError, ValueError) as exc:
            return None, f'冻结证据已无法重建：{exc}'
        if (
            evidence.get('evidence_digest') != pointer.get('evidence_digest')
            or int(evidence.get('scan_max_log_id') or 0)
            != int(pointer.get('scan_max_log_id') or 0)
        ):
            return None, '冻结证据或当前状态已变化，请重新读取后再判断'
        return evidence, None

    @classmethod
    def _historical_review_preview(
        cls,
        admin_user_id: int,
        *,
        evidence: dict,
        review: dict,
        review_source: str,
        review_metadata: dict | None = None,
    ):
        current_max_log_id = int(db_manager.query(
            func.max(AdventurerS1ExpeditionLog.id),
        ).filter(AdventurerS1ExpeditionLog.status == 'completed').scalar() or 0)
        if current_max_log_id != int(evidence.get('scan_max_log_id') or 0):
            return None, '探险日志已变化，请重新读取冻结证据后再判断'

        actions_by_ref = {
            item['action_ref']: item for item in review['actions']
        }
        removed_assets = list(evidence.get('removed_assets') or [])
        adventurer_ids = sorted({
            int(item['adventurer_id']) for item in removed_assets
        })
        trait_names = sorted({
            item['name'] for item in removed_assets
            if item['resource_type'] == 'trait'
        })
        skill_names = sorted({
            item['name'] for item in removed_assets
            if item['resource_type'] == 'skill'
        })
        existing: set[tuple[str, int, str]] = set()
        if adventurer_ids and trait_names:
            existing.update(
                ('trait', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Trait.query.filter(
                    AdventurerS1Trait.adventurer_id.in_(adventurer_ids),
                    AdventurerS1Trait.name.in_(trait_names),
                ).all()
            )
        if adventurer_ids and skill_names:
            existing.update(
                ('skill', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Skill.query.filter(
                    AdventurerS1Skill.adventurer_id.in_(adventurer_ids),
                    AdventurerS1Skill.name.in_(skill_names),
                ).all()
            )

        items = []
        excluded_items = []
        for frozen in removed_assets:
            action = actions_by_ref[frozen['action_ref']]
            key = (
                frozen['resource_type'],
                int(frozen['adventurer_id']),
                frozen['name'],
            )
            reason = None
            if action['decision'] != 'preserve_existing':
                reason = f"review_{action['decision']}"
            elif not frozen.get('snapshot_available'):
                reason = 'frozen_snapshot_missing'
            elif frozen.get('snapshot_conflict'):
                reason = 'frozen_snapshot_conflict'
            elif frozen.get('same_log_changed'):
                reason = 'same_log_structured_change'
            elif frozen.get('later_structured_touches'):
                reason = 'later_structured_change'
            elif key in existing:
                reason = 'currently_present'
            if reason:
                excluded_items.append({
                    **frozen,
                    'expedition_log_id': int(evidence['expedition_log_id']),
                    'later_log_id': (
                        int(frozen['later_structured_touches'][0]['expedition_log_id'])
                        if frozen.get('later_structured_touches')
                        else None
                    ),
                    'review_action': action,
                    'reason': reason,
                })
                continue
            items.append({
                'resource_type': frozen['resource_type'],
                'adventurer_id': int(frozen['adventurer_id']),
                'adventurer_name': frozen['adventurer_name'],
                'name': frozen['name'],
                'description': frozen.get('description') or '',
                'metadata': frozen.get('metadata') or {},
                'incident_log_id': int(evidence['expedition_log_id']),
                'last_evidence_log_id': int(evidence['expedition_log_id']),
                'review_receipt': {
                    'schema_version': HISTORICAL_REVIEW_SCHEMA_VERSION,
                    'evidence_digest': evidence['evidence_digest'],
                    'action': action,
                    'source': review_source,
                    'metadata': dict(review_metadata or {}),
                },
            })

        execution_snapshot = {
            'scan_max_log_id': current_max_log_id,
            'truncated': False,
            'items': items,
            'retry_items': [],
        }
        preview_token = S1AdminScanSnapshotService.store(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            execution_snapshot,
            allow_signed_fallback=True,
        )
        if not preview_token:
            return None, '无法创建历史删除修复预览快照'
        empty_companion_audit = {
            'scanned_logs': 0,
            'scan_limit': 0,
            'scan_truncated': False,
            'reviewed_binding_count': 0,
            'risk_count': 0,
            'mapping_ambiguous_count': 0,
            'stage_risk_count': 0,
            'type_risk_count': 0,
            'description_risk_count': 0,
            'risk_samples': [],
            'risk_samples_truncated': False,
            'calculation_basis': '本预览只处理已冻结的历史最终命途删除项。',
        }
        repair_preview = {
            'scan_max_log_id': current_max_log_id,
            'scanned_logs': 1,
            'incident_logs': 1,
            'truncated': False,
            'candidate_count': len(items),
            'ambiguous_count': len(excluded_items),
            'items': items,
            'ambiguous_items': excluded_items,
            'retry_reopen_count': 0,
            'retry_already_available_count': 0,
            'retry_later_completed_count': 0,
            'retry_ambiguous_count': 0,
            'retry_status_items': [],
            'later_completed_resource_count': 0,
            'later_completed_resource_items': [],
            'unreviewed_scan_logs': 1,
            'unreviewed_destructive_log_count': 0,
            'unreviewed_destructive_member_count': 0,
            'unreviewed_destructive_item_count': 0,
            'unreviewed_current_missing_count': 0,
            'unreviewed_snapshot_missing_count': 0,
            'unreviewed_destructive_logs': [],
            'companion_continuity_audit': empty_companion_audit,
            'preview_token': preview_token,
        }
        return {
            'review': {
                **review,
                'source': review_source,
                'metadata': dict(review_metadata or {}),
            },
            'repair_preview': repair_preview,
            'excluded_items': excluded_items,
        }, None

    @classmethod
    def submit_historical_review(
        cls,
        admin_user_id: int,
        *,
        evidence_token: str,
        raw_review: object,
        review_source: str = 'external',
        review_metadata: dict | None = None,
    ):
        evidence, load_err = cls._load_historical_review_evidence_snapshot(
            admin_user_id,
            evidence_token,
        )
        if load_err:
            return None, load_err
        review, err = validate_historical_review(
            raw_review,
            evidence=evidence,
        )
        if err:
            return None, err
        return cls._historical_review_preview(
            admin_user_id,
            evidence=evidence,
            review=review,
            review_source=str(review_source or 'external')[:64],
            review_metadata=review_metadata,
        )

    @classmethod
    def ai_review_historical_evidence(
        cls,
        admin_user_id: int,
        *,
        evidence_token: str,
    ):
        evidence, load_err = cls._load_historical_review_evidence_snapshot(
            admin_user_id,
            evidence_token,
        )
        if load_err:
            return None, load_err
        from plugins.adventurer_s1.services.s1_fate_historical_review_ai_service import (
            review_historical_fate_evidence,
        )
        review, err, metadata = review_historical_fate_evidence(evidence)
        if err:
            return None, err
        return cls._historical_review_preview(
            admin_user_id,
            evidence=evidence,
            review=review,
            review_source='s1_ai',
            review_metadata={
                key: value
                for key, value in metadata.items()
                if key in {
                    'profile_id',
                    'provider',
                    'model',
                    'thinking_mode',
                    'request_id',
                }
            },
        )

    @classmethod
    def _relevant_rows(
        cls,
        *,
        adventurer_ids: set[int],
        min_log_id: int,
    ) -> list[tuple[int, object]]:
        """批量加载受影响成员从最早事故起参与的日志，供后续变更判定。"""
        if not adventurer_ids:
            return []
        indexed_log_ids = [
            int(row[0])
            for row in (
                db_manager.query(AdventurerS1ExpeditionLogMember.expedition_log_id)
                .join(
                    AdventurerS1ExpeditionLog,
                    AdventurerS1ExpeditionLog.id
                    == AdventurerS1ExpeditionLogMember.expedition_log_id,
                )
                .filter(
                    AdventurerS1ExpeditionLog.status == 'completed',
                    AdventurerS1ExpeditionLog.id >= int(min_log_id),
                    AdventurerS1ExpeditionLogMember.adventurer_id.in_(
                        sorted(adventurer_ids),
                    ),
                )
                .distinct()
                .order_by(AdventurerS1ExpeditionLogMember.expedition_log_id.asc())
                .all()
            )
        ]
        unindexed_log_ids = []
        dialect_name = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .session.get_bind().dialect.name
        )
        if dialect_name == 'sqlite':
            # SQLite 单测及旧本地库可能没有日志成员索引；只在该兼容环境用
            # 精确结构字段片段补扫。PostgreSQL 生产路径只走成员索引。
            full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
            member_id_filters = []
            for adventurer_id in sorted(adventurer_ids):
                member_id_filters.extend((
                    full_log_text.contains(f'"id": {adventurer_id}'),
                    full_log_text.contains(f'"id":{adventurer_id}'),
                ))
            unindexed_log_ids = [
                int(row[0])
                for row in (
                    AdventurerS1ExpeditionLog.query.with_entities(
                        AdventurerS1ExpeditionLog.id,
                    )
                    .filter(
                        AdventurerS1ExpeditionLog.status == 'completed',
                        AdventurerS1ExpeditionLog.id >= int(min_log_id),
                        AdventurerS1ExpeditionLog.cold_compressed_at.is_(None),
                        or_(*member_id_filters),
                    )
                    .all()
                )
            ]
        log_ids = sorted({*indexed_log_ids, *unindexed_log_ids})
        if not log_ids:
            return []
        return (
            AdventurerS1ExpeditionLog.query.with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(AdventurerS1ExpeditionLog.id.in_(log_ids))
            .order_by(AdventurerS1ExpeditionLog.id.asc())
            .all()
        )

    @classmethod
    def _scan(
        cls,
        *,
        expedition_log_id: int | None = None,
        include_unreviewed: bool = False,
    ) -> dict:
        incident_rows = cls._incident_rows(expedition_log_id=expedition_log_id)
        confirmed_unmarked_rows = cls._confirmed_unmarked_rows(
            expedition_log_id=expedition_log_id,
        )
        unreviewed_rows = (
            cls._unreviewed_final_fate_rows(
                expedition_log_id=expedition_log_id,
            )
            if include_unreviewed
            else []
        )
        unreviewed_scan = (
            cls._scan_unreviewed_final_fate_destructive_changes(
                unreviewed_rows,
                excluded_log_ids={
                    *(int(row[0]) for row in incident_rows),
                    *(
                        int(log_id)
                        for log_id in cls.CONFIRMED_UNMARKED_TRAIT_DELETIONS
                    ),
                },
            )
        )
        max_log_id = int(db_manager.query(
            func.max(AdventurerS1ExpeditionLog.id),
        ).filter(AdventurerS1ExpeditionLog.status == 'completed').scalar() or 0)

        candidates: dict[tuple[str, int, str], dict] = {}
        ambiguous = []
        latest_incident_by_adventurer: dict[int, dict] = {}
        for log_id, raw_full_log in incident_rows:
            full_log = raw_full_log if isinstance(raw_full_log, dict) else {}
            context = full_log.get('fate_journey_context')
            rejected = full_log.get('_fate_rejected_reward_manifest')
            if (
                isinstance(context, dict)
                and context.get('event_type') == 'final'
                and isinstance(rejected, dict)
                and rejected
            ):
                try:
                    adventurer_id = int(context.get('adventurer_id') or 0)
                except (TypeError, ValueError):
                    adventurer_id = 0
                try:
                    journey_id = int(context.get('journey_id') or 0)
                except (TypeError, ValueError):
                    journey_id = 0
                if adventurer_id > 0:
                    previous = latest_incident_by_adventurer.get(adventurer_id)
                    if previous is None or int(log_id) > int(previous['incident_log_id']):
                        latest_incident_by_adventurer[adventurer_id] = {
                            'adventurer_id': adventurer_id,
                            'journey_id': journey_id,
                            'incident_log_id': int(log_id),
                        }
                update = cls._member_update(full_log, adventurer_id)
                snapshot = cls._member_snapshot(full_log, adventurer_id)
                character_state = (
                    context.get('character_evidence', {}).get('current_state', {})
                    if isinstance(context.get('character_evidence'), dict)
                    else {}
                )
                if not isinstance(character_state, dict):
                    character_state = {}
                snapshot_skills = {
                    item['name']: item
                    for item in [
                        *cls._named_entries(character_state.get('skills')),
                        *cls._named_entries(snapshot.get('skills')),
                    ]
                }
                snapshot_traits = {
                    item['name']: item
                    for item in [
                        *cls._named_entries(character_state.get('traits')),
                        *cls._named_entries(snapshot.get('traits')),
                    ]
                }
                restore_skills = bool(rejected.get('new_class') or rejected.get('new_skills'))
                restore_traits = bool(rejected.get('new_class') or rejected.get('new_traits'))
                declarations = []
                if restore_skills:
                    declarations.extend(
                        ('skill', name, snapshot_skills.get(name))
                        for name in _normalize_removed_name_list_payload(
                            update.get('removed_skills'),
                        )
                    )
                if restore_traits:
                    declarations.extend(
                        ('trait', name, snapshot_traits.get(name))
                        for name in _normalize_removed_name_list_payload(
                            update.get('removed_traits'),
                        )
                    )
                for resource_type, name, frozen in declarations:
                    if not frozen:
                        ambiguous.append({
                            'expedition_log_id': int(log_id),
                            'adventurer_id': adventurer_id,
                            'resource_type': resource_type,
                            'name': name,
                            'reason': 'frozen_snapshot_missing',
                        })
                        continue
                    candidates[(resource_type, adventurer_id, name)] = {
                        'resource_type': resource_type,
                        'adventurer_id': adventurer_id,
                        'name': name,
                        'description': frozen.get('description') or '',
                        'metadata': frozen.get('metadata') or {},
                        'incident_log_id': int(log_id),
                        'expected_present': True,
                        'last_evidence_log_id': int(log_id),
                    }

        for log_id, raw_full_log in confirmed_unmarked_rows:
            full_log = raw_full_log if isinstance(raw_full_log, dict) else {}
            context = full_log.get('fate_journey_context')
            allowed_members = cls.CONFIRMED_UNMARKED_TRAIT_DELETIONS.get(
                int(log_id),
                {},
            )
            for adventurer_id, allowed_names in allowed_members.items():
                base = {
                    'expedition_log_id': int(log_id),
                    'adventurer_id': int(adventurer_id),
                    'resource_type': 'trait',
                }
                try:
                    context_adventurer_id = int(
                        context.get('adventurer_id') or 0,
                    ) if isinstance(context, dict) else 0
                except (TypeError, ValueError):
                    context_adventurer_id = 0
                if (
                    not isinstance(context, dict)
                    or context.get('event_type') != 'final'
                    or context_adventurer_id != int(adventurer_id)
                ):
                    ambiguous.extend({
                        **base,
                        'name': name,
                        'reason': 'confirmed_incident_context_mismatch',
                    } for name in sorted(allowed_names))
                    continue

                update = cls._member_update(full_log, int(adventurer_id))
                snapshot = cls._member_snapshot(full_log, int(adventurer_id))
                removed = set(_normalize_removed_name_list_payload(
                    update.get('removed_traits'),
                ))
                added = {
                    item['name']
                    for item in cls._named_entries(update.get('new_traits'))
                }
                frozen_traits = {
                    item['name']: item
                    for item in cls._named_entries(snapshot.get('traits'))
                }
                for name in sorted(allowed_names):
                    if name not in removed:
                        ambiguous.append({
                            **base,
                            'name': name,
                            'reason': 'confirmed_removed_trait_missing',
                        })
                        continue
                    if name in added:
                        ambiguous.append({
                            **base,
                            'name': name,
                            'reason': 'same_log_structured_change',
                        })
                        continue
                    frozen = frozen_traits.get(name)
                    if not frozen or not str(
                        frozen.get('description') or '',
                    ).strip():
                        ambiguous.append({
                            **base,
                            'name': name,
                            'reason': 'frozen_snapshot_missing',
                        })
                        continue
                    candidates[('trait', int(adventurer_id), name)] = {
                        'resource_type': 'trait',
                        'adventurer_id': int(adventurer_id),
                        'name': name,
                        'description': frozen['description'],
                        'metadata': frozen.get('metadata') or {},
                        'incident_log_id': int(log_id),
                        'expected_present': True,
                        'last_evidence_log_id': int(log_id),
                    }

        relevant_rows = cls._relevant_rows(
            adventurer_ids={
                int(item['adventurer_id']) for item in candidates.values()
            },
            min_log_id=min(
                (int(item['incident_log_id']) for item in candidates.values()),
                default=max_log_id + 1,
            ),
        )
        for log_id, raw_full_log in relevant_rows:
            full_log = raw_full_log if isinstance(raw_full_log, dict) else {}
            # Any later structured touch makes attribution non-unique.  Do not auto-repair
            # it from this incident, even when the later row would otherwise look additive.
            for key, item in list(candidates.items()):
                if int(log_id) <= int(item['incident_log_id']):
                    continue
                update = cls._member_update(full_log, int(item['adventurer_id']))
                if not update:
                    continue
                name = item['name']
                if item['resource_type'] == 'skill':
                    removed = set(_normalize_removed_name_list_payload(update.get('removed_skills')))
                    added = {
                        str(row.get('name') or '').strip(): row
                        for row in _normalize_new_skills_payload(
                            update.get('new_skills'),
                            split_semantic_name_desc=False,
                        )
                        if str(row.get('name') or '').strip()
                    }
                else:
                    removed = set(_normalize_removed_name_list_payload(update.get('removed_traits')))
                    added = {row['name']: row for row in cls._named_entries(update.get('new_traits'))}
                if name in removed or name in added:
                    ambiguous.append({
                        'expedition_log_id': int(item['incident_log_id']),
                        'adventurer_id': int(item['adventurer_id']),
                        'resource_type': item['resource_type'],
                        'name': name,
                        'reason': 'later_structured_change',
                        'later_log_id': int(log_id),
                    })
                    candidates.pop(key, None)

        expected = list(candidates.values())
        adv_ids = sorted({int(item['adventurer_id']) for item in expected})
        skill_names = sorted({
            item['name'] for item in expected if item['resource_type'] == 'skill'
        })
        trait_names = sorted({
            item['name'] for item in expected if item['resource_type'] == 'trait'
        })
        existing = set()
        if adv_ids and skill_names:
            existing.update(
                ('skill', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Skill.query.filter(
                    AdventurerS1Skill.adventurer_id.in_(adv_ids),
                    AdventurerS1Skill.name.in_(skill_names),
                ).all()
            )
        if adv_ids and trait_names:
            existing.update(
                ('trait', int(row.adventurer_id), str(row.name))
                for row in AdventurerS1Trait.query.filter(
                    AdventurerS1Trait.adventurer_id.in_(adv_ids),
                    AdventurerS1Trait.name.in_(trait_names),
                ).all()
            )
        adv_names = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adv_ids)).all()
        ) if adv_ids else {}
        items = []
        for item in expected:
            key = (item['resource_type'], int(item['adventurer_id']), item['name'])
            if key in existing:
                continue
            result = dict(item)
            result.pop('expected_present', None)
            result['adventurer_name'] = adv_names.get(
                int(item['adventurer_id']),
                f"#{item['adventurer_id']}",
            )
            items.append(result)
        items.sort(key=lambda item: (
            int(item['incident_log_id']),
            int(item['adventurer_id']),
            item['resource_type'],
            item['name'],
        ))

        retry_adventurer_ids = sorted(latest_incident_by_adventurer)
        retry_adv_names = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(retry_adventurer_ids)).all()
        ) if retry_adventurer_ids else {}
        journeys = (
            AdventurerS1FateJourney.query.filter(
                AdventurerS1FateJourney.adventurer_id.in_(
                    retry_adventurer_ids,
                ),
            ).all()
            if retry_adventurer_ids
            else []
        )
        journey_by_adventurer = {
            int(row.adventurer_id): row for row in journeys
        }
        retry_items = []
        retry_status_items = []
        for adventurer_id, incident in sorted(
            latest_incident_by_adventurer.items(),
            key=lambda row: int(row[1]['incident_log_id']),
        ):
            journey = journey_by_adventurer.get(int(adventurer_id))
            base = {
                **incident,
                'adventurer_name': retry_adv_names.get(
                    int(adventurer_id),
                    f'#{adventurer_id}',
                ),
            }
            if journey is None:
                retry_status_items.append({
                    **base,
                    'retry_status': 'ambiguous',
                    'reason': 'journey_missing',
                })
                continue
            if (
                int(incident.get('journey_id') or 0) > 0
                and int(journey.id) != int(incident['journey_id'])
            ):
                retry_status_items.append({
                    **base,
                    'current_journey_id': int(journey.id),
                    'retry_status': 'ambiguous',
                    'reason': 'journey_replaced',
                })
                continue
            final_log_id = int(journey.final_expedition_log_id or 0)
            if (
                bool(journey.breakthrough_completed)
                and final_log_id > int(incident['incident_log_id'])
            ):
                retry_status_items.append({
                    **base,
                    'current_journey_id': int(journey.id),
                    'retry_status': 'later_completed',
                    'later_final_log_id': final_log_id,
                })
                continue
            if (
                journey.status == 'final_ready'
                and not bool(journey.breakthrough_completed)
            ):
                retry_status_items.append({
                    **base,
                    'current_journey_id': int(journey.id),
                    'retry_status': 'already_available',
                })
                continue
            if (
                journey.status == 'completed'
                and final_log_id <= int(incident['incident_log_id'])
            ):
                retry_item = {
                    **base,
                    'current_journey_id': int(journey.id),
                    'retry_status': 'reopen',
                    'expected_state': cls._journey_retry_state(journey),
                }
                retry_items.append(retry_item)
                retry_status_items.append(retry_item)
                continue
            retry_status_items.append({
                **base,
                'current_journey_id': int(journey.id),
                'retry_status': 'ambiguous',
                'reason': 'later_journey_state_changed',
                'current_status': str(journey.status or ''),
            })

        retry_counts = {
            status: sum(
                item.get('retry_status') == status
                for item in retry_status_items
            )
            for status in (
                'reopen',
                'already_available',
                'later_completed',
                'ambiguous',
            )
        }
        later_completed_adventurer_ids = {
            int(item['adventurer_id'])
            for item in retry_status_items
            if item.get('retry_status') == 'later_completed'
        }
        later_completed_resource_items = [
            item for item in items
            if int(item['adventurer_id']) in later_completed_adventurer_ids
        ]
        if later_completed_adventurer_ids:
            # A later successful final can legitimately replace, merge, rename,
            # or supersede old abilities. Exact-name history cannot prove that
            # the current absence is still damage from the earlier failed final.
            items = [
                item for item in items
                if int(item['adventurer_id'])
                not in later_completed_adventurer_ids
            ]
        if include_unreviewed or expedition_log_id is not None:
            from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
                S1CompanionEvolutionStageRepairService,
            )
            companion_continuity_audit = (
                S1CompanionEvolutionStageRepairService
                ._scan_expedition_continuity_audit(
                    expedition_log_id=expedition_log_id,
                )
            )
        else:
            companion_continuity_audit = {
                'scanned_logs': 0,
                'scan_limit': 0,
                'scan_truncated': False,
                'reviewed_binding_count': 0,
                'risk_count': 0,
                'mapping_ambiguous_count': 0,
                'stage_risk_count': 0,
                'type_risk_count': 0,
                'description_risk_count': 0,
                'risk_samples': [],
                'risk_samples_truncated': False,
                'calculation_basis': (
                    '轻量预览未扫描伙伴替换；限定日志或使用一键历史风险扫描后展示。'
                ),
            }
        return {
            'scan_max_log_id': max_log_id,
            'scanned_logs': len({
                *(int(row[0]) for row in incident_rows),
                *(int(row[0]) for row in confirmed_unmarked_rows),
                *(int(row[0]) for row in relevant_rows),
                *(int(row[0]) for row in unreviewed_rows),
            }),
            'incident_logs': len({
                *(int(row[0]) for row in incident_rows),
                *(int(row[0]) for row in confirmed_unmarked_rows),
            }),
            'truncated': False,
            'candidate_count': len(items),
            'ambiguous_count': len(ambiguous),
            'items': items,
            'ambiguous_items': ambiguous,
            'retry_reopen_count': retry_counts['reopen'],
            'retry_already_available_count': retry_counts['already_available'],
            'retry_later_completed_count': retry_counts['later_completed'],
            'retry_ambiguous_count': retry_counts['ambiguous'],
            'retry_items': retry_items,
            'retry_status_items': retry_status_items,
            'later_completed_resource_count': len(
                later_completed_resource_items,
            ),
            'later_completed_resource_items': later_completed_resource_items,
            'companion_continuity_audit': companion_continuity_audit,
            **unreviewed_scan,
        }

    @classmethod
    def preview(
        cls,
        admin_user_id: int,
        *,
        expedition_log_id: int | None = None,
        include_unreviewed: bool = False,
    ):
        try:
            snapshot = cls._scan(
                expedition_log_id=expedition_log_id,
                include_unreviewed=include_unreviewed,
            )
            execution_snapshot = dict(snapshot)
            execution_snapshot.pop('unreviewed_destructive_logs', None)
            execution_snapshot.pop('companion_continuity_audit', None)
            preview_token = S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_OPERATION,
                admin_user_id,
                execution_snapshot,
                allow_signed_fallback=True,
            )
            result = {
                key: value
                for key, value in snapshot.items()
                if key != 'retry_items'
            }
            result['preview_token'] = preview_token
            return result, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 命途半套结算历史扫描失败')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        preview = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        if preview.get('truncated'):
            return None, '历史日志超过安全扫描上限，未覆盖全量记录，禁止执行自动修复'
        items = preview.get('items') or []
        retry_items = preview.get('retry_items') or []
        if not items and not retry_items:
            return None, '当前没有需要写入的命途修复'
        created = []
        reopened_journeys = []
        skipped = 0
        retry_skipped = 0
        run_id = _portable_utc_now().strftime('%Y%m%dT%H%M%S%fZ')
        try:
            with db_manager.transaction():
                adv_ids = sorted({
                    int(item['adventurer_id'])
                    for item in [*items, *retry_items]
                })
                locked_ids = {
                    int(row.id)
                    for row in AdventurerS1.query.filter(
                        AdventurerS1.id.in_(adv_ids),
                    ).order_by(AdventurerS1.id.asc()).with_for_update().all()
                } if adv_ids else set()
                current_max_log_id = int(db_manager.query(
                    func.max(AdventurerS1ExpeditionLog.id),
                ).filter(AdventurerS1ExpeditionLog.status == 'completed').scalar() or 0)
                if current_max_log_id != int(preview.get('scan_max_log_id') or 0):
                    return None, '探险日志已变化，预览快照过期，请重新预览'

                retry_journey_ids = sorted({
                    int(item.get('current_journey_id') or 0)
                    for item in retry_items
                    if int(item.get('current_journey_id') or 0) > 0
                })
                locked_journeys = {
                    int(row.id): row
                    for row in AdventurerS1FateJourney.query.filter(
                        AdventurerS1FateJourney.id.in_(retry_journey_ids),
                    ).order_by(
                        AdventurerS1FateJourney.adventurer_id.asc(),
                    ).with_for_update().all()
                } if retry_journey_ids else {}

                skill_names = [item['name'] for item in items if item['resource_type'] == 'skill']
                trait_names = [item['name'] for item in items if item['resource_type'] == 'trait']
                existing = set()
                if adv_ids and skill_names:
                    existing.update(
                        ('skill', int(row.adventurer_id), str(row.name))
                        for row in AdventurerS1Skill.query.filter(
                            AdventurerS1Skill.adventurer_id.in_(adv_ids),
                            AdventurerS1Skill.name.in_(skill_names),
                        ).all()
                    )
                if adv_ids and trait_names:
                    existing.update(
                        ('trait', int(row.adventurer_id), str(row.name))
                        for row in AdventurerS1Trait.query.filter(
                            AdventurerS1Trait.adventurer_id.in_(adv_ids),
                            AdventurerS1Trait.name.in_(trait_names),
                        ).all()
                    )
                for item in items:
                    resource_type = str(item['resource_type'])
                    adventurer_id = int(item['adventurer_id'])
                    key = (resource_type, adventurer_id, str(item['name']))
                    if adventurer_id not in locked_ids or key in existing:
                        skipped += 1
                        continue
                    if resource_type == 'skill':
                        row = AdventurerS1Skill(
                            adventurer_id=adventurer_id,
                            name=item['name'],
                            description=item.get('description') or '',
                            equipped=True,
                        )
                    else:
                        metadata = item.get('metadata') or {}
                        row = AdventurerS1Trait(
                            adventurer_id=adventurer_id,
                            name=item['name'],
                            description=item.get('description') or '',
                            recovery_policy=metadata.get('recovery_policy') or 'none',
                            recovery_stage=metadata.get('recovery_stage'),
                            recovery_progress=int(metadata.get('recovery_progress') or 0),
                            recovery_required=metadata.get('recovery_required'),
                        )
                    db_manager.add(row)
                    db_manager.flush()
                    record = {
                        **item,
                        'created_id': int(row.id),
                        'created_state': cls._created_state(row, resource_type),
                        'run_id': run_id,
                    }
                    created.append(record)
                    existing.add(key)

                opened_state = cls._opened_final_retry_state()
                for item in retry_items:
                    adventurer_id = int(item['adventurer_id'])
                    journey_id = int(item.get('current_journey_id') or 0)
                    journey = locked_journeys.get(journey_id)
                    expected_state = item.get('expected_state')
                    if (
                        adventurer_id not in locked_ids
                        or journey is None
                        or int(journey.adventurer_id) != adventurer_id
                        or not isinstance(expected_state, dict)
                        or cls._journey_retry_state(journey) != expected_state
                    ):
                        retry_skipped += 1
                        continue
                    cls._apply_journey_retry_state(journey, opened_state)
                    reopened_journeys.append({
                        'run_id': run_id,
                        'journey_id': journey_id,
                        'adventurer_id': adventurer_id,
                        'adventurer_name': item.get('adventurer_name') or '',
                        'incident_log_id': int(item['incident_log_id']),
                        'before_state': expected_state,
                        'opened_state': dict(opened_state),
                    })

                setting = AdventurerS1Settings.query.filter_by(
                    key=cls.BACKUP_KEY,
                ).with_for_update().first()
                history = {'runs': []}
                if setting and setting.value:
                    try:
                        loaded = json.loads(setting.value)
                        if isinstance(loaded, dict):
                            history = loaded
                    except (TypeError, ValueError):
                        pass
                runs = list(history.get('runs') or [])
                runs.append({
                    'run_id': run_id,
                    'created_at': _portable_utc_now().isoformat() + 'Z',
                    'admin_user_id': int(admin_user_id),
                    'entries': created,
                    'journey_entries': reopened_journeys,
                })
                payload = json.dumps({'runs': runs[-50:]}, ensure_ascii=False)
                if setting is None:
                    db_manager.add(AdventurerS1Settings(
                        key=cls.BACKUP_KEY,
                        value=payload,
                        description='命途半套结算旧能力恢复与最终试炼重开记录',
                        updated_by_user_id=admin_user_id,
                    ))
                else:
                    setting.value = payload
                    setting.updated_by_user_id = admin_user_id
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION,
                admin_user_id,
                preview_token,
            )
            cls._invalidate_adventurer_details({
                int(item['adventurer_id'])
                for item in [*created, *reopened_journeys]
            })
            return {
                'run_id': run_id,
                'created_count': len(created),
                'skipped_count': skipped,
                'items': created,
                'reopened_final_count': len(reopened_journeys),
                'retry_skipped_count': retry_skipped,
                'reopened_journeys': reopened_journeys,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 命途半套结算历史修复失败')
            return None, f'修复失败：{exc}'

    @classmethod
    def restore(cls, *, run_id: str = ''):
        run_id = str(run_id or '').strip()
        if not run_id:
            return None, '必须提供要回滚的命途修复批次号'
        setting = AdventurerS1Settings.query.filter_by(key=cls.BACKUP_KEY).first()
        if not setting or not setting.value:
            return None, '没有可回滚的命途半套结算修复记录'
        try:
            history = json.loads(setting.value)
            runs = list(history.get('runs') or [])
        except (TypeError, ValueError, AttributeError):
            return None, '命途修复记录已损坏，无法回滚'
        target = next((
            row for row in reversed(runs)
            if row.get('run_id') == run_id
        ), None)
        if target is None:
            return None, '没有找到指定的命途修复批次'
        removed = 0
        skipped = 0
        restored_journeys = 0
        journey_skipped = 0
        changed_adventurer_ids: set[int] = set()
        try:
            with db_manager.transaction():
                all_entries = [
                    *(target.get('entries') or []),
                    *(target.get('journey_entries') or []),
                ]
                adventurer_ids = sorted({
                    int(item['adventurer_id'])
                    for item in all_entries
                    if int(item.get('adventurer_id') or 0) > 0
                })
                locked_adventurer_ids = {
                    int(row.id)
                    for row in AdventurerS1.query.filter(
                        AdventurerS1.id.in_(adventurer_ids),
                    ).order_by(
                        AdventurerS1.id.asc(),
                    ).with_for_update().all()
                } if adventurer_ids else set()
                for item in target.get('entries') or []:
                    adventurer_id = int(item['adventurer_id'])
                    model = (
                        AdventurerS1Skill
                        if item.get('resource_type') == 'skill'
                        else AdventurerS1Trait
                    )
                    row = model.query.filter_by(id=int(item['created_id'])).with_for_update().first()
                    resource_type = str(item.get('resource_type') or '')
                    expected_state = item.get('created_state')
                    if not isinstance(expected_state, dict):
                        expected_state = cls._legacy_created_state(item)
                    if (
                        row is None
                        or adventurer_id not in locked_adventurer_ids
                        or int(row.adventurer_id) != adventurer_id
                        or row.name != item['name']
                        or cls._created_state(row, resource_type) != expected_state
                    ):
                        skipped += 1
                        continue
                    db_manager.delete_instance(row)
                    removed += 1
                    changed_adventurer_ids.add(adventurer_id)
                journey_entries = target.get('journey_entries') or []
                journey_ids = sorted({
                    int(item.get('journey_id') or 0)
                    for item in journey_entries
                    if int(item.get('journey_id') or 0) > 0
                })
                journeys = {
                    int(row.id): row
                    for row in AdventurerS1FateJourney.query.filter(
                        AdventurerS1FateJourney.id.in_(journey_ids),
                    ).order_by(
                        AdventurerS1FateJourney.adventurer_id.asc(),
                    ).with_for_update().all()
                } if journey_ids else {}
                for item in journey_entries:
                    adventurer_id = int(item['adventurer_id'])
                    journey = journeys.get(int(item.get('journey_id') or 0))
                    opened_state = item.get('opened_state')
                    before_state = item.get('before_state')
                    if (
                        adventurer_id not in locked_adventurer_ids
                        or journey is None
                        or int(journey.adventurer_id) != adventurer_id
                        or not isinstance(opened_state, dict)
                        or not isinstance(before_state, dict)
                        or cls._journey_retry_state(journey) != opened_state
                    ):
                        journey_skipped += 1
                        continue
                    cls._apply_journey_retry_state(journey, before_state)
                    restored_journeys += 1
                    changed_adventurer_ids.add(adventurer_id)
                target['restored_at'] = _portable_utc_now().isoformat() + 'Z'
                target['restored_count'] = removed
                target['restored_journey_count'] = restored_journeys
                setting.value = json.dumps({'runs': runs[-50:]}, ensure_ascii=False)
            cls._invalidate_adventurer_details(changed_adventurer_ids)
            return {
                'run_id': target.get('run_id'),
                'removed_count': removed,
                'skipped_count': skipped,
                'restored_journey_count': restored_journeys,
                'journey_skipped_count': journey_skipped,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 命途半套结算历史修复回滚失败')
            return None, f'回滚失败：{exc}'
