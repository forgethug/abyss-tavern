"""AI-reviewed, reversible lineage binding for historical companion continuity."""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from typing import Iterable

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BugReclaimBatch,
    AdventurerS1BugReclaimItem,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1MarketListing,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_companion_historical_review_contract import (
    MAX_ACTIONS,
    SCHEMA_VERSION,
    canonical_digest,
    stable_ref,
    validate_companion_historical_review,
)
from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
    S1CompanionEvolutionStageRepairService,
)


class S1CompanionHistoricalRepairService:
    """Freeze evidence, review identity, bind lineage, and recheck exact rows."""

    EVIDENCE_OPERATION = 'companion_historical_review_evidence_v2'
    PREVIEW_OPERATION = 'companion_historical_lineage_preview_v2'
    ISSUE_TYPE = 'companion_continuity_repair'
    ISSUE_KEY_PREFIX = 'companion_continuity:v2:'
    MAX_LATER_LOGS = 32
    MAX_EVIDENCE_RECORDS = 128
    MAX_EVENT_TEXT_LENGTH = 2000

    @staticmethod
    def _text(value: object) -> str:
        return str(value or '').strip()

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _timestamp(value: object) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    @classmethod
    def action_ref(cls, expedition_log_id: int, adventurer_id: int, consumed_name: str) -> str:
        return stable_ref(
            'cmp',
            int(expedition_log_id),
            int(adventurer_id),
            consumed_name,
        )

    @classmethod
    def issue_key(cls, action_ref: str) -> str:
        return f'{cls.ISSUE_KEY_PREFIX}{action_ref}'

    @classmethod
    def active_reviewed_action_refs(cls) -> set[str]:
        rows = db_manager.query(AdventurerS1BugReclaimItem.issue_key).filter(
            AdventurerS1BugReclaimItem.issue_type == cls.ISSUE_TYPE,
            AdventurerS1BugReclaimItem.status == 'executed',
            AdventurerS1BugReclaimItem.reverted_at.is_(None),
        ).all()
        prefix = cls.ISSUE_KEY_PREFIX
        return {
            str(row.issue_key)[len(prefix):]
            for row in rows
            if str(row.issue_key or '').startswith(prefix)
        }

    @classmethod
    def load_active_lineage_context(cls, adventurer_ids: Iterable[int]) -> dict[int, list[dict]]:
        """Batch-load active reviewed same-entity bindings for prompt snapshots."""
        normalized_ids = set()
        for value in adventurer_ids:
            if isinstance(value, bool):
                continue
            try:
                adventurer_id = int(value)
            except (TypeError, ValueError):
                continue
            if adventurer_id > 0:
                normalized_ids.add(adventurer_id)
        if not normalized_ids:
            return {}
        rows = db_manager.query(AdventurerS1BugReclaimItem).filter(
            AdventurerS1BugReclaimItem.adventurer_id.in_(normalized_ids),
            AdventurerS1BugReclaimItem.issue_type == cls.ISSUE_TYPE,
            AdventurerS1BugReclaimItem.status == 'executed',
            AdventurerS1BugReclaimItem.reverted_at.is_(None),
            AdventurerS1BugReclaimItem.issue_key.like(f'{cls.ISSUE_KEY_PREFIX}%'),
        ).order_by(AdventurerS1BugReclaimItem.id.asc()).all()
        result: dict[int, list[dict]] = {}
        for row in rows:
            receipt = row.result_json if isinstance(row.result_json, dict) else {}
            context = receipt.get('lineage_context')
            if not isinstance(context, dict):
                continue
            target = context.get('current_target')
            if (
                context.get('decision') != 'bind_descendant'
                or not isinstance(target, dict)
                or target.get('source') not in {'equipment', 'warehouse', 'market_listing'}
                or cls._nonnegative_int(target.get('target_id')) <= 0
            ):
                continue
            adventurer_id = int(row.adventurer_id)
            result.setdefault(adventurer_id, []).append({
                key: context.get(key)
                for key in (
                    'schema_version', 'decision', 'action_ref',
                    'source_expedition_log_id', 'adventurer_id', 'predecessor',
                    'source_successor', 'evolution_chain', 'current_target',
                )
            })
        return result

    @classmethod
    def _event_text(cls, raw_event: object) -> str:
        if isinstance(raw_event, str):
            return raw_event.strip()
        if not isinstance(raw_event, dict):
            return ''
        for key in ('content', 'description', 'text', 'narrative', 'event'):
            text = cls._text(raw_event.get(key))
            if text:
                return text
        return ''

    @classmethod
    def _event_records(cls, log_id: int, full_log: dict) -> list[dict]:
        records = []
        for event_index, raw_event in enumerate(full_log.get('events') or []):
            text = cls._event_text(raw_event)
            if not text:
                continue
            text = text[:cls.MAX_EVENT_TEXT_LENGTH]
            records.append({
                'evidence_ref': stable_ref('evt', log_id, event_index, text),
                'expedition_log_id': int(log_id),
                'event_index': event_index,
                'text': text,
            })
        return records

    @classmethod
    def _snapshot_for(cls, full_log: dict, adventurer_id: int) -> dict | None:
        rows = [
            row for row in full_log.get('audit_member_snapshot') or []
            if isinstance(row, dict)
            and cls._nonnegative_int(row.get('adventurer_id')) == adventurer_id
        ]
        return rows[0] if len(rows) == 1 else None

    @classmethod
    def _member_update_for(cls, full_log: dict, adventurer_id: int) -> dict | None:
        rows = [
            row for row in full_log.get('member_updates') or []
            if isinstance(row, dict)
            and cls._nonnegative_int(row.get('id')) == adventurer_id
        ]
        return rows[0] if len(rows) == 1 else None

    @classmethod
    def _consumed_quantities(cls, update: dict) -> dict[str, int]:
        result: dict[str, int] = {}
        raw_rows = update.get('consumed_equipments')
        rows = raw_rows if isinstance(raw_rows, list) else [raw_rows]
        for raw in rows:
            if isinstance(raw, dict):
                name = cls._text(raw.get('name'))
                quantity = max(1, cls._nonnegative_int(raw.get('quantity')))
            elif isinstance(raw, str):
                name = raw.strip()
                quantity = 1
            else:
                continue
            if name:
                result[name] = result.get(name, 0) + quantity
        return result

    @classmethod
    def _new_items(cls, log_id: int, adventurer_id: int, update: dict) -> list[dict]:
        raw_items = update.get('new_equipments')
        if not isinstance(raw_items, dict):
            return []
        items = []
        for position, (raw_name, raw_metadata) in enumerate(raw_items.items()):
            name = cls._text(raw_name)
            if not name:
                continue
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            description = cls._text(
                metadata.get('desc') or metadata.get('description'),
            )
            items.append({
                'item_ref': stable_ref('new', log_id, adventurer_id, position, name),
                'name': name,
                'type': cls._text(metadata.get('type')),
                'rarity': cls._text(metadata.get('rarity')),
                'evolution_stage': cls._nonnegative_int(metadata.get('evolution_stage')),
                'description': description,
            })
        return items

    @classmethod
    def _departure_companions(cls, snapshot: dict) -> list[dict]:
        result = []
        for raw in snapshot.get('equipment') or []:
            if not isinstance(raw, dict) or cls._text(raw.get('type')) != 'companion':
                continue
            name = cls._text(raw.get('name'))
            if not name:
                continue
            result.append({
                'name': name,
                'type': 'companion',
                'rarity': cls._text(raw.get('rarity')),
                'evolution_stage': cls._nonnegative_int(raw.get('evolution_stage')),
                'quantity': max(1, cls._nonnegative_int(raw.get('quantity'))),
                'description': cls._text(raw.get('description') or raw.get('desc')),
            })
        return result

    @classmethod
    def _row_state(cls, source: str, row: object) -> dict:
        is_listing = source == 'market_listing'
        adventurer_id = (
            int(row.seller_adventurer_id)
            if is_listing else int(row.adventurer_id)
        )
        name = str(row.item_name if is_listing else row.name)
        item_type = str(row.item_type if is_listing else row.type)
        state = {
            'source': source,
            'target_id': int(row.id),
            'adventurer_id': adventurer_id,
            'name': name,
            'type': item_type,
            'rarity': str(row.rarity or ''),
            'current_custom_desc': str(row.custom_desc or ''),
            'current_stage': max(0, int(row.evolution_stage or 0)),
            'updated_at': cls._timestamp(getattr(row, 'updated_at', None)),
        }
        state['item_ref'] = stable_ref(
            'cur',
            source,
            state['target_id'],
            state['adventurer_id'],
            canonical_digest(state),
        )
        return state

    @classmethod
    def _current_items(cls, adventurer_id: int) -> list[dict]:
        rows = S1CompanionEvolutionStageRepairService._load_current_companions(
            {int(adventurer_id)},
        )
        return [cls._row_state(source, row) for source, row in rows]

    @classmethod
    def _later_timeline(cls, log: AdventurerS1ExpeditionLog, adventurer_id: int) -> tuple[list[dict], list[dict], bool]:
        rows = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id
                == AdventurerS1ExpeditionLog.id,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id == adventurer_id,
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.id > int(log.id),
            )
            .order_by(AdventurerS1ExpeditionLog.id.asc())
            .limit(cls.MAX_LATER_LOGS + 1)
            .all()
        )
        truncated = len(rows) > cls.MAX_LATER_LOGS
        timeline = []
        evidence_records = []
        for later in rows[:cls.MAX_LATER_LOGS]:
            full_log = later.full_log if isinstance(later.full_log, dict) else {}
            snapshot = cls._snapshot_for(full_log, adventurer_id)
            update = cls._member_update_for(full_log, adventurer_id)
            if snapshot is None or update is None:
                continue
            new_items = cls._new_items(int(later.id), adventurer_id, update)
            consumed = cls._consumed_quantities(update)
            departure = cls._departure_companions(snapshot)
            if not consumed and not any(item.get('type') == 'companion' for item in new_items):
                continue
            timeline.append({
                'expedition_log_id': int(later.id),
                'created_at': cls._timestamp(later.created_at),
                'departure_companions': departure,
                'consumed_equipments': consumed,
                'new_items': new_items,
            })
            remaining = cls.MAX_EVIDENCE_RECORDS - len(evidence_records)
            if remaining <= 0:
                truncated = True
                continue
            later_events = cls._event_records(int(later.id), full_log)
            if len(later_events) > remaining:
                truncated = True
            evidence_records.extend(later_events[:remaining])
        return timeline, evidence_records, truncated

    @classmethod
    def _evolution_logs(
        cls,
        log: AdventurerS1ExpeditionLog,
        adventurer_id: int,
    ) -> tuple[list[dict], bool]:
        rows = []
        for raw in S1CompanionEvolutionStageRepairService._iter_successful_evolution_logs():
            try:
                if int(raw.get('adventurer_id') or 0) != adventurer_id:
                    continue
                created_at = raw.get('created_at')
                if log.created_at and created_at and created_at <= log.created_at:
                    continue
                result = raw.get('result') if isinstance(raw.get('result'), dict) else {}
                old_name = cls._text(result.get('old_name'))
                new_name = cls._text(result.get('new_name'))
                if not old_name or not new_name:
                    continue
                log_id = int(raw['id'])
            except (KeyError, TypeError, ValueError):
                continue
            before = result.get('pre_evolution_snapshot')
            before = before if isinstance(before, dict) else {}
            rows.append({
                'evolution_log_ref': stable_ref('evo', log_id, adventurer_id, old_name, new_name),
                'usage_log_id': log_id,
                'created_at': cls._timestamp(created_at),
                'old_name': old_name,
                'new_name': new_name,
                'recorded_before_stage': cls._nonnegative_int(before.get('evolution_stage')),
                'recorded_after_stage': cls._nonnegative_int(result.get('evolution_stage')),
                'before_description': cls._text(before.get('custom_desc')),
                'after_description': cls._text(
                    ((result.get('_undo_snapshot') or {}).get('after') or {}).get('custom_desc')
                    if isinstance(result.get('_undo_snapshot'), dict) else '',
                ),
            })
        return rows[:64], len(rows) > 64

    @classmethod
    def _case_from_log(cls, log: AdventurerS1ExpeditionLog, sample: dict) -> dict:
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        adventurer_id = int(sample['adventurer_id'])
        consumed_name = cls._text(sample.get('consumed_name'))
        snapshot = cls._snapshot_for(full_log, adventurer_id)
        update = cls._member_update_for(full_log, adventurer_id)
        if snapshot is None or update is None:
            raise ValueError(f'探险日志 #{log.id} 的成员冻结快照或结算记录不唯一')
        departure_rows = [
            item for item in cls._departure_companions(snapshot)
            if item['name'] == consumed_name
        ]
        if len(departure_rows) != 1:
            raise ValueError(f'探险日志 #{log.id} 的旧伙伴冻结实例不唯一')
        consumed = cls._consumed_quantities(update)
        if consumed_name not in consumed:
            raise ValueError(f'探险日志 #{log.id} 已不再包含该旧伙伴消耗')

        later_timeline, later_evidence, timeline_truncated = cls._later_timeline(
            log,
            adventurer_id,
        )
        source_evidence = cls._event_records(int(log.id), full_log)
        evidence_records = source_evidence + later_evidence
        if len(evidence_records) > cls.MAX_EVIDENCE_RECORDS:
            evidence_records = evidence_records[:cls.MAX_EVIDENCE_RECORDS]
            timeline_truncated = True
        evolution_logs, evolution_logs_truncated = cls._evolution_logs(
            log,
            adventurer_id,
        )
        return {
            'action_ref': cls.action_ref(int(log.id), adventurer_id, consumed_name),
            'expedition_log_id': int(log.id),
            'party_name': cls._text(log.party_name),
            'created_at': cls._timestamp(log.created_at),
            'adventurer_id': adventurer_id,
            'adventurer_name': cls._text(snapshot.get('adventurer_name')),
            'consumed_name': consumed_name,
            'consumed_quantity': int(consumed[consumed_name]),
            'departure_item': dict(departure_rows[0]),
            'departure_stage': int(departure_rows[0]['evolution_stage']),
            'departure_description': departure_rows[0]['description'],
            'source_new_items': cls._new_items(int(log.id), adventurer_id, update),
            'evidence_records': evidence_records,
            'later_timeline': later_timeline,
            'evolution_logs': evolution_logs,
            'current_items': cls._current_items(adventurer_id),
            'timeline_truncated': bool(
                timeline_truncated or evolution_logs_truncated
            ),
        }

    @classmethod
    def _build_evidence(cls, expedition_log_ids: Iterable[int]) -> dict:
        normalized_ids = []
        for value in expedition_log_ids:
            log_id = int(value)
            if log_id <= 0 or log_id in normalized_ids:
                continue
            normalized_ids.append(log_id)
        if not normalized_ids:
            raise ValueError('没有可复核的探险日志')
        if len(normalized_ids) > MAX_ACTIONS:
            raise ValueError(f'单次最多复核 {MAX_ACTIONS} 条伙伴连续性记录')
        logs = AdventurerS1ExpeditionLog.query.filter(
            AdventurerS1ExpeditionLog.id.in_(normalized_ids),
            AdventurerS1ExpeditionLog.status == 'completed',
        ).all()
        logs_by_id = {int(log.id): log for log in logs}
        if set(logs_by_id) != set(normalized_ids):
            raise ValueError('部分探险日志不存在或尚未完成')

        cases = []
        excluded_cases = []
        active_refs = cls.active_reviewed_action_refs()
        for log_id in normalized_ids:
            audit = S1CompanionEvolutionStageRepairService._scan_expedition_continuity_audit(
                expedition_log_id=log_id,
            )
            samples = [
                sample for sample in audit.get('risk_samples') or []
                if cls.action_ref(
                    log_id,
                    int(sample.get('adventurer_id') or 0),
                    cls._text(sample.get('consumed_name')),
                ) not in active_refs
            ]
            if not samples:
                continue
            for sample in samples:
                action_ref = cls.action_ref(
                    log_id,
                    int(sample.get('adventurer_id') or 0),
                    cls._text(sample.get('consumed_name')),
                )
                if len(cases) >= MAX_ACTIONS:
                    excluded_cases.append({
                        'action_ref': action_ref,
                        'expedition_log_id': log_id,
                        'adventurer_id': int(sample.get('adventurer_id') or 0),
                        'consumed_name': cls._text(sample.get('consumed_name')),
                        'reason': f'超过单批 {MAX_ACTIONS} 项自动复核上限',
                    })
                    continue
                try:
                    cases.append(cls._case_from_log(logs_by_id[log_id], sample))
                except (TypeError, ValueError) as exc:
                    excluded_cases.append({
                        'action_ref': action_ref,
                        'expedition_log_id': log_id,
                        'adventurer_id': int(sample.get('adventurer_id') or 0),
                        'consumed_name': cls._text(sample.get('consumed_name')),
                        'reason': str(exc),
                    })
        if not cases:
            if excluded_cases:
                raise ValueError(
                    f'所选日志的 {len(excluded_cases)} 项伙伴连续性风险均无法冻结完整证据'
                )
            raise ValueError('所选日志没有尚未处理的伙伴连续性风险')
        evidence = {
            'schema_version': SCHEMA_VERSION,
            'first_expedition_log_id': min(item['expedition_log_id'] for item in cases),
            'requested_expedition_log_ids': normalized_ids,
            'cases': cases,
            'excluded_cases': excluded_cases,
        }
        evidence['evidence_digest'] = canonical_digest(evidence)
        return evidence

    @classmethod
    def load_review_evidence(cls, admin_user_id: int, expedition_log_ids: list[int]):
        try:
            evidence = cls._build_evidence(expedition_log_ids)
            pointer = {
                'expedition_log_ids': list(evidence['requested_expedition_log_ids']),
                'evidence_digest': evidence['evidence_digest'],
            }
            token = S1AdminScanSnapshotService.store(
                cls.EVIDENCE_OPERATION,
                admin_user_id,
                pointer,
                allow_signed_fallback=True,
            )
            if not token:
                return None, '无法创建历史伙伴连续性证据快照'
            return {'evidence_token': token, 'evidence': evidence}, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 历史伙伴连续性证据读取失败')
            return None, f'读取复核证据失败：{exc}'

    @classmethod
    def _load_evidence_snapshot(cls, admin_user_id: int, token: str):
        pointer = S1AdminScanSnapshotService.load(
            cls.EVIDENCE_OPERATION,
            admin_user_id,
            token,
            allow_signed_fallback=True,
        )
        if pointer is None:
            return None, '复核证据快照已过期，请重新读取'
        try:
            evidence = cls._build_evidence(pointer.get('expedition_log_ids') or [])
        except (TypeError, ValueError) as exc:
            return None, f'冻结证据已无法重建：{exc}'
        if evidence.get('evidence_digest') != pointer.get('evidence_digest'):
            return None, '冻结证据或当前伙伴状态已变化，请重新读取后再判断'
        return evidence, None

    @classmethod
    def _target_stage(cls, case: dict, action: dict) -> int:
        successor = next(
            item for item in case['source_new_items']
            if item['item_ref'] == action['source_successor_ref']
        )
        selected = {
            item['evolution_log_ref']: item
            for item in case.get('evolution_logs') or []
            if item['evolution_log_ref'] in set(action['evolution_log_refs'])
        }
        stage = max(
            int(case.get('departure_stage') or 0),
            int(successor.get('evolution_stage') or 0),
        )
        for ref in action['evolution_log_refs']:
            row = selected[ref]
            stage = max(stage + 1, int(row.get('recorded_after_stage') or 0))
        return stage

    @classmethod
    def _lineage_context(cls, case: dict, action: dict, current: dict) -> dict:
        successor = next(
            item for item in case['source_new_items']
            if item['item_ref'] == action['source_successor_ref']
        )
        selected_evolution_refs = set(action['evolution_log_refs'])
        selected_evidence_refs = set(action['evidence_refs'])
        return {
            'schema_version': SCHEMA_VERSION,
            'decision': 'bind_descendant',
            'action_ref': action['action_ref'],
            'source_expedition_log_id': int(case['expedition_log_id']),
            'adventurer_id': int(case['adventurer_id']),
            'predecessor': dict(case.get('departure_item') or {
                'name': case['consumed_name'],
                'type': 'companion',
                'rarity': '',
                'evolution_stage': int(case.get('departure_stage') or 0),
                'quantity': int(case.get('consumed_quantity') or 1),
                'description': cls._text(case.get('departure_description')),
            }),
            'source_successor': dict(successor),
            'evolution_chain': [
                dict(item) for item in case.get('evolution_logs') or []
                if item.get('evolution_log_ref') in selected_evolution_refs
            ],
            'current_target': {
                key: current.get(key)
                for key in (
                    'source', 'target_id', 'adventurer_id', 'name', 'type',
                    'rarity', 'current_custom_desc', 'current_stage',
                )
            },
            'evidence_records': [
                dict(item) for item in case.get('evidence_records') or []
                if item.get('evidence_ref') in selected_evidence_refs
            ],
        }

    @classmethod
    def _review_preview(
        cls,
        admin_user_id: int,
        *,
        evidence: dict,
        review: dict,
        review_source: str,
        review_metadata: dict | None = None,
    ):
        cases_by_ref = {item['action_ref']: item for item in evidence['cases']}
        executable = []
        excluded = [
            {
                **item,
                'decision': 'insufficient_evidence',
            }
            for item in evidence.get('excluded_cases') or []
        ]
        for action in review['actions']:
            case = cases_by_ref[action['action_ref']]
            if action['decision'] == 'insufficient_evidence':
                excluded.append({
                    'action_ref': action['action_ref'],
                    'expedition_log_id': case['expedition_log_id'],
                    'adventurer_id': case['adventurer_id'],
                    'adventurer_name': case['adventurer_name'],
                    'consumed_name': case['consumed_name'],
                    'decision': action['decision'],
                    'reason': action['reason'],
                })
                continue
            item = {
                'action_ref': action['action_ref'],
                'issue_key': cls.issue_key(action['action_ref']),
                'mode': 'review_only',
                'expedition_log_id': case['expedition_log_id'],
                'adventurer_id': case['adventurer_id'],
                'adventurer_name': case['adventurer_name'],
                'consumed_name': case['consumed_name'],
                'decision': action['decision'],
                'reason': action['reason'],
                'review_receipt': {
                    'schema_version': SCHEMA_VERSION,
                    'evidence_digest': evidence['evidence_digest'],
                    'action': action,
                    'source': review_source,
                    'metadata': dict(review_metadata or {}),
                },
            }
            if action['decision'] == 'bind_descendant':
                current = next(
                    row for row in case['current_items']
                    if row['item_ref'] == action['current_item_ref']
                )
                target_stage = max(current['current_stage'], cls._target_stage(case, action))
                lineage_context = cls._lineage_context(case, action, current)
                item.update({
                    'target': current,
                    'target_stage': target_stage,
                    'source_successor_ref': action['source_successor_ref'],
                    'evolution_log_refs': action['evolution_log_refs'],
                })
                item['review_receipt']['lineage_context'] = lineage_context
                item['mode'] = (
                    'stage_repair'
                    if target_stage > current['current_stage']
                    else 'bind_only'
                )
            executable.append(item)
        snapshot = {
            'evidence_digest': evidence['evidence_digest'],
            'items': executable,
            'excluded_items': excluded,
        }
        preview_token = S1AdminScanSnapshotService.store(
            cls.PREVIEW_OPERATION,
            admin_user_id,
            snapshot,
            allow_signed_fallback=True,
        )
        if not preview_token:
            return None, '无法创建伙伴连续性谱系预览快照'
        binding_count = sum(
            item['mode'] in {'bind_only', 'stage_repair'} for item in executable
        )
        stage_repair_count = sum(item['mode'] == 'stage_repair' for item in executable)
        return {
            'review': {**review, 'source': review_source, 'metadata': dict(review_metadata or {})},
            'repair_preview': {
                'preview_token': preview_token,
                'candidate_count': binding_count,
                'binding_count': binding_count,
                'stage_repair_count': stage_repair_count,
                'reviewed_noop_count': len(executable) - binding_count,
                'unresolved_count': len(excluded),
                'items': executable,
                'excluded_items': excluded,
            },
        }, None

    @classmethod
    def ai_review_preview(cls, admin_user_id: int, evidence_token: str):
        evidence, err = cls._load_evidence_snapshot(admin_user_id, evidence_token)
        if err:
            return None, err
        from plugins.adventurer_s1.services.s1_companion_historical_review_ai_service import (
            review_historical_companion_evidence,
        )
        review, err, metadata = review_historical_companion_evidence(evidence)
        if err:
            return None, err
        return cls._review_preview(
            admin_user_id,
            evidence=evidence,
            review=review,
            review_source='s1_ai',
            review_metadata={
                key: value for key, value in metadata.items()
                if key in {'profile_id', 'provider', 'model', 'thinking_mode', 'request_id'}
            },
        )

    @classmethod
    def submit_review(cls, admin_user_id: int, evidence_token: str, raw_review: object):
        evidence, err = cls._load_evidence_snapshot(admin_user_id, evidence_token)
        if err:
            return None, err
        review, err = validate_companion_historical_review(raw_review, evidence=evidence)
        if err:
            return None, err
        return cls._review_preview(
            admin_user_id,
            evidence=evidence,
            review=review,
            review_source='external_api',
        )

    @classmethod
    def _target_model(cls, source: str):
        return {
            'equipment': AdventurerS1Equipment,
            'warehouse': AdventurerS1WarehouseItem,
            'market_listing': AdventurerS1MarketListing,
        }.get(source)

    @classmethod
    def _load_target(cls, target: dict, *, lock: bool):
        source = str(target.get('source') or '')
        model = cls._target_model(source)
        if model is None:
            return None
        query = db_manager.query(model).filter(model.id == int(target['target_id']))
        if source == 'market_listing':
            query = query.filter(
                model.seller_adventurer_id == int(target['adventurer_id']),
                model.status == 'active',
            )
        else:
            query = query.filter(model.adventurer_id == int(target['adventurer_id']))
        if lock:
            query = query.with_for_update()
        return query.first()

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        preview = S1AdminScanSnapshotService.load(
            cls.PREVIEW_OPERATION,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '谱系预览已过期，请重新点击检查'
        items = list(preview.get('items') or [])
        if not items:
            return {
                'bound_count': 0,
                'stage_repaired_count': 0,
                'repaired_count': 0,
                'reviewed_count': 0,
            }, None
        affected_ids: set[int] = set()
        bound = 0
        stage_repaired = 0
        reviewed = 0
        owner_by_adventurer_id = {
            int(row.id): (int(row.owner_id) if row.owner_id is not None else None)
            for row in db_manager.query(AdventurerS1.id, AdventurerS1.owner_id).filter(
                AdventurerS1.id.in_({int(item['adventurer_id']) for item in items}),
            ).all()
        }
        try:
            with db_manager.transaction():
                batch = AdventurerS1BugReclaimBatch(
                    admin_user_id=admin_user_id,
                    status='executed',
                    criteria_json={
                        'issue_type': cls.ISSUE_TYPE,
                        'evidence_digest': preview.get('evidence_digest'),
                    },
                    summary_json={},
                    note='历史伙伴连续性 AI 复核与谱系绑定',
                    executed_at=_portable_utc_now(),
                )
                ok, add_err = db_manager.add(batch)
                if not ok:
                    raise RuntimeError(add_err or '无法创建伙伴连续性谱系批次')
                for item in sorted(items, key=lambda row: (row['adventurer_id'], row['action_ref'])):
                    existing = db_manager.query(AdventurerS1BugReclaimItem).filter_by(
                        issue_key=item['issue_key'],
                        status='executed',
                    ).with_for_update().first()
                    if existing is not None and existing.reverted_at is None:
                        continue
                    before_state = None
                    after_state = None
                    if item['mode'] in {'bind_only', 'stage_repair'}:
                        target = item['target']
                        row = cls._load_target(target, lock=True)
                        if row is None:
                            raise RuntimeError(f"伙伴实例 {target['item_ref']} 已不存在")
                        current_state = cls._row_state(target['source'], row)
                        if current_state != target:
                            raise RuntimeError(f"伙伴实例 {target['item_ref']} 状态已变化")
                        bound += 1
                        if item['mode'] == 'stage_repair':
                            before_state = dict(current_state)
                            row.evolution_stage = max(
                                int(row.evolution_stage or 0),
                                int(item['target_stage']),
                            )
                            after_state = {
                                **before_state,
                                'current_stage': int(row.evolution_stage),
                            }
                            after_state.pop('item_ref', None)
                            after_state.pop('updated_at', None)
                            before_state.pop('item_ref', None)
                            before_state.pop('updated_at', None)
                            affected_ids.add(int(item['adventurer_id']))
                            stage_repaired += 1
                    owner_id = owner_by_adventurer_id.get(int(item['adventurer_id']))
                    values = {
                        'batch': batch,
                        'status': 'executed',
                        'issue_key': item['issue_key'],
                        'issue_type': cls.ISSUE_TYPE,
                        'audit_log_id': int(item['expedition_log_id']),
                        'adventurer_id': int(item['adventurer_id']),
                        'user_id': int(owner_id) if owner_id is not None else None,
                        'item_name': str(item['consumed_name']),
                        'item_type': 'companion',
                        'item_rarity': str((item.get('target') or {}).get('rarity') or 'common'),
                        'illegal_quantity': 0,
                        'reclaimed_item_quantity': 0,
                        'gold_deducted': 0.0,
                        'before_json': before_state,
                        'after_json': after_state,
                        'route_json': {'mode': item['mode']},
                        'result_json': item['review_receipt'],
                        'executed_by': admin_user_id,
                        'executed_at': _portable_utc_now(),
                        'reverted_by': None,
                        'reverted_at': None,
                        'revert_result_json': None,
                        'error_message': None,
                    }
                    if existing is None:
                        ok, add_err = db_manager.add(AdventurerS1BugReclaimItem(**values))
                        if not ok:
                            raise RuntimeError(add_err or '无法写入伙伴连续性复核回执')
                    else:
                        for key, value in values.items():
                            setattr(existing, key, value)
                    reviewed += 1
                batch.summary_json = {
                    'reviewed_count': reviewed,
                    'bound_count': bound,
                    'stage_repaired_count': stage_repaired,
                }
            S1AdminScanSnapshotService.delete(
                cls.PREVIEW_OPERATION,
                admin_user_id,
                preview_token,
            )
            from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
                S1CompanionEvolutionIntegrityScanService,
            )
            S1CompanionEvolutionIntegrityScanService.mark_continuity_logs_dirty({
                int(item['expedition_log_id']) for item in items
            })
            S1CompanionEvolutionStageRepairService._invalidate_adventurers(affected_ids)
            return {
                'run_id': str(batch.id),
                'reviewed_count': reviewed,
                'bound_count': bound,
                'stage_repaired_count': stage_repaired,
                'repaired_count': stage_repaired,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 历史伙伴连续性谱系绑定失败')
            return None, f'伙伴连续性谱系绑定失败：{exc}'

    @classmethod
    def restore(cls, admin_user_id: int, run_id: str):
        try:
            batch_id = int(run_id)
        except (TypeError, ValueError):
            return None, 'run_id 无效'
        batch = db_manager.query(AdventurerS1BugReclaimBatch).filter_by(
            id=batch_id,
        ).first()
        if not batch or (batch.criteria_json or {}).get('issue_type') != cls.ISSUE_TYPE:
            return None, '找不到对应的伙伴连续性修复批次'
        affected_ids: set[int] = set()
        restored = 0
        stage_restored = 0
        stage_skipped = 0
        affected_expedition_log_ids: set[int] = set()
        try:
            with db_manager.transaction():
                rows = db_manager.query(AdventurerS1BugReclaimItem).filter(
                    AdventurerS1BugReclaimItem.batch_id == batch_id,
                    AdventurerS1BugReclaimItem.issue_type == cls.ISSUE_TYPE,
                    AdventurerS1BugReclaimItem.status == 'executed',
                    AdventurerS1BugReclaimItem.reverted_at.is_(None),
                ).order_by(AdventurerS1BugReclaimItem.id.asc()).with_for_update().all()
                for ledger in rows:
                    if ledger.audit_log_id is not None:
                        affected_expedition_log_ids.add(int(ledger.audit_log_id))
                    stage_restored_for_row = False
                    before = ledger.before_json if isinstance(ledger.before_json, dict) else None
                    after = ledger.after_json if isinstance(ledger.after_json, dict) else None
                    if before is not None and after is not None:
                        target = {
                            'source': after.get('source'),
                            'target_id': after.get('target_id'),
                            'adventurer_id': after.get('adventurer_id'),
                        }
                        row = cls._load_target(target, lock=True)
                        if row is None:
                            stage_skipped += 1
                        else:
                            current_state = cls._row_state(str(after['source']), row)
                            comparable = dict(current_state)
                            comparable.pop('item_ref', None)
                            comparable.pop('updated_at', None)
                            if comparable == after:
                                row.evolution_stage = int(before['current_stage'])
                                affected_ids.add(int(ledger.adventurer_id))
                                stage_restored += 1
                                stage_restored_for_row = True
                            else:
                                stage_skipped += 1
                    ledger.reverted_by = admin_user_id
                    ledger.reverted_at = _portable_utc_now()
                    ledger.revert_result_json = {
                        'status': 'lineage_removed',
                        'stage_restored': stage_restored_for_row,
                    }
                    restored += 1
                batch.status = 'dismissed'
            from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
                S1CompanionEvolutionIntegrityScanService,
            )
            S1CompanionEvolutionIntegrityScanService.mark_continuity_logs_dirty(
                affected_expedition_log_ids,
            )
            S1CompanionEvolutionStageRepairService._invalidate_adventurers(affected_ids)
            return {
                'restored_count': restored,
                'stage_restored_count': stage_restored,
                'stage_skipped_count': stage_skipped,
                'skipped_count': stage_skipped,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 历史伙伴谱系移除失败')
            return None, f'伙伴谱系移除失败：{exc}'


__all__ = ['S1CompanionHistoricalRepairService']
