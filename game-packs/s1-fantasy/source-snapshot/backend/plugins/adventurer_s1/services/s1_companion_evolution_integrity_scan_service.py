"""Persisted, resumable preview scan for companion evolution integrity."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import current_app
from sqlalchemy import func, text

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1BugReclaimItem,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1BuildingUsageLog,
    AdventurerS1CompanionEvolutionScanEvent,
    AdventurerS1CompanionEvolutionScanState,
    AdventurerS1ExpeditionLog,
    AdventurerS1MarketBuyOffer,
    AdventurerS1MarketBuyOrder,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
)


_DETECTION_MODE = 'companion_evolution_integrity_v1_resumable'
_SCAN_STATE_ID = 1
_SCAN_ADVISORY_LOCK_KEY = 23408882923933516
_ORDERED_SOURCES = ('evolution', 'listing', 'barter', 'buy_offer', 'continuity')
_EVENT_SOURCE_TYPES = {
    'evolution': 'evolution_log',
    'listing': 'market_listing_transfer',
    'barter': 'market_barter_offer_transfer',
    'buy_offer': 'market_buy_offer_transfer',
    'continuity': 'expedition_continuity',
}


class S1CompanionEvolutionIntegrityScanService:
    """Advance one bounded source page per request and reuse persisted history."""

    @staticmethod
    def _lock_scan_transaction() -> None:
        db_uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI') or '')
        if 'postgresql' not in db_uri:
            return
        from extensions import db

        db.session.execute(
            text('SELECT pg_advisory_xact_lock(:key)'),
            {'key': _SCAN_ADVISORY_LOCK_KEY},
        )

    @staticmethod
    def _iso(value: object) -> Optional[str]:
        return value.isoformat() if isinstance(value, datetime) else None

    @classmethod
    def _live_snapshot(cls) -> dict:
        from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
            _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT,
        )

        evolution_max = int(
            db_manager.query(func.max(AdventurerS1BuildingUsageLog.id))
            .scalar() or 0
        )

        listing_filter = (
            AdventurerS1MarketListing.status == 'completed',
            AdventurerS1MarketListing.item_type == 'companion',
            AdventurerS1MarketListing.buyer_adventurer_id.isnot(None),
            AdventurerS1MarketListing.evolution_stage > 0,
        )
        listing_max, listing_updated, listing_count = (
            db_manager.query(
                func.max(AdventurerS1MarketListing.id),
                func.max(AdventurerS1MarketListing.updated_at),
                func.count(AdventurerS1MarketListing.id),
            ).filter(*listing_filter).one()
        )

        barter_filter = (
            AdventurerS1MarketOffer.status == 'accepted',
            AdventurerS1MarketOffer.offered_item_type == 'companion',
            AdventurerS1MarketOffer.offered_evolution_stage.isnot(None),
            AdventurerS1MarketOffer.offered_evolution_stage > 0,
        )
        barter_max, barter_updated, barter_count = (
            db_manager.query(
                func.max(AdventurerS1MarketOffer.id),
                func.max(AdventurerS1MarketOffer.updated_at),
                func.count(AdventurerS1MarketOffer.id),
            ).filter(*barter_filter).one()
        )

        buy_filter = (
            AdventurerS1MarketBuyOffer.status == 'accepted',
            AdventurerS1MarketBuyOffer.item_type == 'companion',
            AdventurerS1MarketBuyOffer.evolution_stage.isnot(None),
            AdventurerS1MarketBuyOffer.evolution_stage > 0,
        )
        buy_max, buy_updated, buy_count = (
            db_manager.query(
                func.max(AdventurerS1MarketBuyOffer.id),
                func.max(AdventurerS1MarketBuyOffer.updated_at),
                func.count(AdventurerS1MarketBuyOffer.id),
            ).filter(*buy_filter).one()
        )

        recent_expedition_ids = [
            int(row[0])
            for row in (
                db_manager.query(AdventurerS1ExpeditionLog.id)
                .filter(AdventurerS1ExpeditionLog.status == 'completed')
                .order_by(AdventurerS1ExpeditionLog.id.desc())
                .limit(_EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT + 1)
                .all()
            )
        ]
        selected_expedition_ids = recent_expedition_ids[
            :_EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT
        ]
        continuity_max = selected_expedition_ids[0] if selected_expedition_ids else 0
        continuity_min = selected_expedition_ids[-1] if selected_expedition_ids else 0
        continuity_updated = (
            db_manager.query(func.max(AdventurerS1ExpeditionLog.updated_at))
            .filter(
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.id >= continuity_min,
                AdventurerS1ExpeditionLog.id <= continuity_max,
            ).scalar()
            if selected_expedition_ids else None
        )
        audit_filter = (
            AdventurerS1BugReclaimItem.issue_type.in_((
                'companion_evolution_fee_evasion',
                'companion_evolution_fee_reconciliation',
            )),
            AdventurerS1BugReclaimItem.status == 'executed',
            AdventurerS1BugReclaimItem.reverted_at.is_(None),
        )
        audit_max, audit_updated, audit_count = (
            db_manager.query(
                func.max(AdventurerS1BugReclaimItem.id),
                func.max(AdventurerS1BugReclaimItem.updated_at),
                func.count(AdventurerS1BugReclaimItem.id),
            ).filter(*audit_filter).one()
        )

        altar_config_revision = [
            [int(instance_id), config]
            for instance_id, config in (
                db_manager.query(
                    AdventurerS1BuildingInstance.id,
                    AdventurerS1BuildingTemplate.settlement_config,
                )
                .join(
                    AdventurerS1BuildingTemplate,
                    AdventurerS1BuildingTemplate.id
                    == AdventurerS1BuildingInstance.template_id,
                )
                .filter(AdventurerS1BuildingTemplate.handler_key == 'pet_evolution')
                .order_by(AdventurerS1BuildingInstance.id.asc())
                .all()
            )
        ]

        return {
            'targets': {
                'evolution': evolution_max,
                'listing': int(listing_max or 0),
                'barter': int(barter_max or 0),
                'buy_offer': int(buy_max or 0),
                'continuity': int(continuity_max or 0),
                'continuity_min': int(continuity_min or 0),
                'continuity_truncated': (
                    len(recent_expedition_ids)
                    > _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT
                ),
            },
            'revisions': {
                'listing': [int(listing_count or 0), cls._iso(listing_updated)],
                'barter': [int(barter_count or 0), cls._iso(barter_updated)],
                'buy_offer': [int(buy_count or 0), cls._iso(buy_updated)],
                'continuity': [
                    len(selected_expedition_ids),
                    int(continuity_min or 0),
                    int(continuity_max or 0),
                    cls._iso(continuity_updated),
                ],
                'fee_audit': [int(audit_count or 0), int(audit_max or 0), cls._iso(audit_updated)],
                'altar_config': altar_config_revision,
            },
        }

    @staticmethod
    def _initial_cursors(targets: dict) -> dict[str, int]:
        return {
            'evolution': 0,
            'listing': 0,
            'barter': 0,
            'buy_offer': 0,
            'continuity': max(0, int(targets.get('continuity_min') or 0) - 1),
        }

    @classmethod
    def _new_state(cls, live: dict) -> AdventurerS1CompanionEvolutionScanState:
        targets = {**live['targets'], 'scan_mode': 'full_baseline'}
        return AdventurerS1CompanionEvolutionScanState(
            id=_SCAN_STATE_ID,
            detection_mode=_DETECTION_MODE,
            status='scanning',
            source_cursors_json=cls._initial_cursors(targets),
            source_targets_json=targets,
            source_revisions_json=dict(live['revisions']),
            dirty_expedition_log_ids_json=[],
            cached_result_json=None,
        )

    @classmethod
    def _reset_source(
        cls,
        state: AdventurerS1CompanionEvolutionScanState,
        source: str,
        live: dict,
    ) -> None:
        event_type = _EVENT_SOURCE_TYPES[source]
        db_manager.query(AdventurerS1CompanionEvolutionScanEvent).filter_by(
            source_type=event_type,
        ).delete(synchronize_session=False)
        cursors = dict(state.source_cursors_json or {})
        targets = dict(state.source_targets_json or {})
        targets[source] = int(live['targets'].get(source) or 0)
        cursors[source] = (
            max(0, int(live['targets'].get('continuity_min') or 0) - 1)
            if source == 'continuity' else 0
        )
        state.source_cursors_json = cursors
        state.source_targets_json = targets

    @classmethod
    def _reconcile_ready_state(
        cls,
        state: AdventurerS1CompanionEvolutionScanState,
        live: dict,
    ) -> bool:
        """Return True when persisted artifacts remain current."""
        targets = dict(state.source_targets_json or {})
        cursors = dict(state.source_cursors_json or {})
        revisions = dict(state.source_revisions_json or {})
        live_targets = live['targets']
        live_revisions = live['revisions']

        if int(live_targets['evolution']) < int(targets.get('evolution') or 0):
            db_manager.query(AdventurerS1CompanionEvolutionScanEvent).delete(
                synchronize_session=False,
            )
            replacement = cls._new_state(live)
            state.detection_mode = replacement.detection_mode
            state.status = replacement.status
            state.source_cursors_json = replacement.source_cursors_json
            state.source_targets_json = replacement.source_targets_json
            state.source_revisions_json = replacement.source_revisions_json
            state.dirty_expedition_log_ids_json = []
            state.cached_result_json = None
            return False

        changed = False
        if int(live_targets['evolution']) > int(targets.get('evolution') or 0):
            targets['evolution'] = int(live_targets['evolution'])
            changed = True

        for source in ('listing', 'barter', 'buy_offer'):
            if live_revisions[source] != revisions.get(source):
                cls._reset_source(state, source, live)
                cursors = dict(state.source_cursors_json or {})
                targets = dict(state.source_targets_json or {})
                changed = True

        old_continuity_max = int(targets.get('continuity') or 0)
        new_continuity_max = int(live_targets.get('continuity') or 0)
        if new_continuity_max > old_continuity_max:
            new_min = int(live_targets.get('continuity_min') or 0)
            db_manager.query(AdventurerS1CompanionEvolutionScanEvent).filter(
                AdventurerS1CompanionEvolutionScanEvent.source_type
                == _EVENT_SOURCE_TYPES['continuity'],
                AdventurerS1CompanionEvolutionScanEvent.source_id < new_min,
            ).delete(synchronize_session=False)
            cursors['continuity'] = max(old_continuity_max, new_min - 1)
            targets['continuity'] = new_continuity_max
            targets['continuity_min'] = new_min
            targets['continuity_truncated'] = bool(
                live_targets.get('continuity_truncated')
            )
            changed = True
        elif live_revisions['continuity'] != revisions.get('continuity'):
            cls._reset_source(state, 'continuity', live)
            cursors = dict(state.source_cursors_json or {})
            targets = dict(state.source_targets_json or {})
            targets['continuity_min'] = int(live_targets.get('continuity_min') or 0)
            targets['continuity_truncated'] = bool(
                live_targets.get('continuity_truncated')
            )
            changed = True

        derived_changed = (
            live_revisions['fee_audit'] != revisions.get('fee_audit')
            or live_revisions['altar_config'] != revisions.get('altar_config')
        )
        if changed or derived_changed or state.dirty_expedition_log_ids_json:
            targets['scan_mode'] = 'incremental'
            state.source_cursors_json = cursors
            state.source_targets_json = targets
            state.source_revisions_json = dict(live_revisions)
            state.status = 'scanning' if changed or state.dirty_expedition_log_ids_json else 'finalizing'
            state.cached_result_json = None
            return False
        return True

    @classmethod
    def _prepare(cls) -> dict:
        with db_manager.transaction():
            cls._lock_scan_transaction()
            state = (
                db_manager.query(AdventurerS1CompanionEvolutionScanState)
                .filter_by(id=_SCAN_STATE_ID)
                .with_for_update()
                .first()
            )
            if state is None or state.detection_mode != _DETECTION_MODE:
                live = cls._live_snapshot()
                db_manager.query(AdventurerS1CompanionEvolutionScanEvent).delete(
                    synchronize_session=False,
                )
                replacement = cls._new_state(live)
                if state is None:
                    state = replacement
                    db_manager.add(state)
                else:
                    state.detection_mode = replacement.detection_mode
                    state.status = replacement.status
                    state.source_cursors_json = replacement.source_cursors_json
                    state.source_targets_json = replacement.source_targets_json
                    state.source_revisions_json = replacement.source_revisions_json
                    state.dirty_expedition_log_ids_json = []
                    state.cached_result_json = None
            elif state.status == 'ready':
                live = cls._live_snapshot()
                if cls._reconcile_ready_state(state, live):
                    return {'action': 'ready'}

            if state.status == 'finalizing':
                return {'action': 'finalize'}

            dirty_ids = sorted({
                int(log_id)
                for log_id in (state.dirty_expedition_log_ids_json or [])
                if int(log_id) > 0
            })
            if dirty_ids:
                batch_size = max(1, int(current_app.config.get(
                    'ADVENTURER_S1_COMPANION_EVOLUTION_SCAN_EXPEDITION_BATCH_SIZE',
                    50,
                )))
                return {
                    'action': 'batch',
                    'source': 'continuity_dirty',
                    'dirty_ids': dirty_ids[:batch_size],
                    'detection_mode': state.detection_mode,
                }

            cursors = dict(state.source_cursors_json or {})
            targets = dict(state.source_targets_json or {})
            for source in _ORDERED_SOURCES:
                cursor = int(cursors.get(source) or 0)
                target = int(targets.get(source) or 0)
                if cursor < target:
                    return {
                        'action': 'batch',
                        'source': source,
                        'cursor': cursor,
                        'target': target,
                        'continuity_min': int(targets.get('continuity_min') or 0),
                        'detection_mode': state.detection_mode,
                    }
            state.status = 'finalizing'
            return {'action': 'finalize'}

    @classmethod
    def _read_batch(cls, spec: dict) -> tuple[list[dict], int, list[int]]:
        source = str(spec['source'])
        cursor = int(spec.get('cursor') or 0)
        target = int(spec.get('target') or 0)
        event_batch_size = max(1, int(current_app.config.get(
            'ADVENTURER_S1_COMPANION_EVOLUTION_SCAN_EVENT_BATCH_SIZE', 500,
        )))
        expedition_batch_size = max(1, int(current_app.config.get(
            'ADVENTURER_S1_COMPANION_EVOLUTION_SCAN_EXPEDITION_BATCH_SIZE', 50,
        )))
        processed_ids: list[int] = []

        if source == 'evolution':
            rows = (
                db_manager.query(
                    AdventurerS1BuildingUsageLog.id,
                    AdventurerS1BuildingUsageLog.adventurer_id,
                    AdventurerS1BuildingUsageLog.building_instance_id,
                    AdventurerS1BuildingUsageLog.result,
                    AdventurerS1BuildingUsageLog.created_at,
                )
                .filter(
                    AdventurerS1BuildingUsageLog.id > cursor,
                    AdventurerS1BuildingUsageLog.id <= target,
                    AdventurerS1BuildingUsageLog.result['type'].as_string() == 'pet_evolution',
                    AdventurerS1BuildingUsageLog.result['success'].as_boolean().is_(True),
                )
                .order_by(AdventurerS1BuildingUsageLog.id.asc())
                .limit(event_batch_size).all()
            )
            events = [{
                'source_type': _EVENT_SOURCE_TYPES[source],
                'source_id': int(row.id),
                'event_at': row.created_at,
                'payload': {
                    'id': int(row.id),
                    'adventurer_id': int(row.adventurer_id),
                    'building_instance_id': int(row.building_instance_id),
                    'result': row.result,
                },
            } for row in rows]
        elif source == 'listing':
            rows = (
                db_manager.query(
                    AdventurerS1MarketListing.id,
                    AdventurerS1MarketListing.seller_adventurer_id,
                    AdventurerS1MarketListing.buyer_adventurer_id,
                    AdventurerS1MarketListing.item_name,
                    AdventurerS1MarketListing.evolution_stage,
                    AdventurerS1MarketListing.completed_at,
                )
                .filter(
                    AdventurerS1MarketListing.id > cursor,
                    AdventurerS1MarketListing.id <= target,
                    AdventurerS1MarketListing.status == 'completed',
                    AdventurerS1MarketListing.item_type == 'companion',
                    AdventurerS1MarketListing.buyer_adventurer_id.isnot(None),
                    AdventurerS1MarketListing.evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketListing.id.asc())
                .limit(event_batch_size).all()
            )
            events = [{
                'source_type': _EVENT_SOURCE_TYPES[source],
                'source_id': int(row.id),
                'event_at': row.completed_at,
                'payload': {
                    'event_type': _EVENT_SOURCE_TYPES[source],
                    'event_id': int(row.id),
                    'from_adventurer_id': int(row.seller_adventurer_id),
                    'to_adventurer_id': int(row.buyer_adventurer_id),
                    'item_name': str(row.item_name),
                    'stage': max(0, int(row.evolution_stage or 0)),
                },
            } for row in rows]
        elif source == 'barter':
            rows = (
                db_manager.query(
                    AdventurerS1MarketOffer.id,
                    AdventurerS1MarketOffer.buyer_adventurer_id,
                    AdventurerS1MarketListing.seller_adventurer_id,
                    AdventurerS1MarketOffer.offered_item_name,
                    AdventurerS1MarketOffer.offered_evolution_stage,
                    AdventurerS1MarketOffer.updated_at,
                )
                .join(
                    AdventurerS1MarketListing,
                    AdventurerS1MarketListing.id == AdventurerS1MarketOffer.listing_id,
                )
                .filter(
                    AdventurerS1MarketOffer.id > cursor,
                    AdventurerS1MarketOffer.id <= target,
                    AdventurerS1MarketOffer.status == 'accepted',
                    AdventurerS1MarketOffer.offered_item_type == 'companion',
                    AdventurerS1MarketOffer.offered_evolution_stage.isnot(None),
                    AdventurerS1MarketOffer.offered_evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketOffer.id.asc())
                .limit(event_batch_size).all()
            )
            events = [{
                'source_type': _EVENT_SOURCE_TYPES[source],
                'source_id': int(row.id),
                'event_at': row.updated_at,
                'payload': {
                    'event_type': _EVENT_SOURCE_TYPES[source],
                    'event_id': int(row.id),
                    'from_adventurer_id': int(row.buyer_adventurer_id),
                    'to_adventurer_id': int(row.seller_adventurer_id),
                    'item_name': str(row.offered_item_name),
                    'stage': max(0, int(row.offered_evolution_stage or 0)),
                },
            } for row in rows]
        elif source == 'buy_offer':
            rows = (
                db_manager.query(
                    AdventurerS1MarketBuyOffer.id,
                    AdventurerS1MarketBuyOffer.seller_adventurer_id,
                    AdventurerS1MarketBuyOrder.buyer_adventurer_id,
                    AdventurerS1MarketBuyOffer.item_name,
                    AdventurerS1MarketBuyOffer.evolution_stage,
                    AdventurerS1MarketBuyOffer.updated_at,
                )
                .join(
                    AdventurerS1MarketBuyOrder,
                    AdventurerS1MarketBuyOrder.id == AdventurerS1MarketBuyOffer.buy_order_id,
                )
                .filter(
                    AdventurerS1MarketBuyOffer.id > cursor,
                    AdventurerS1MarketBuyOffer.id <= target,
                    AdventurerS1MarketBuyOffer.status == 'accepted',
                    AdventurerS1MarketBuyOffer.item_type == 'companion',
                    AdventurerS1MarketBuyOffer.evolution_stage.isnot(None),
                    AdventurerS1MarketBuyOffer.evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketBuyOffer.id.asc())
                .limit(event_batch_size).all()
            )
            events = [{
                'source_type': _EVENT_SOURCE_TYPES[source],
                'source_id': int(row.id),
                'event_at': row.updated_at,
                'payload': {
                    'event_type': _EVENT_SOURCE_TYPES[source],
                    'event_id': int(row.id),
                    'from_adventurer_id': int(row.seller_adventurer_id),
                    'to_adventurer_id': int(row.buyer_adventurer_id),
                    'item_name': str(row.item_name),
                    'stage': max(0, int(row.evolution_stage or 0)),
                },
            } for row in rows]
        else:
            dirty = source == 'continuity_dirty'
            dirty_ids = [int(log_id) for log_id in spec.get('dirty_ids') or []]
            query = db_manager.query(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.party_name,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.created_at,
            ).filter(AdventurerS1ExpeditionLog.status == 'completed')
            if dirty:
                processed_ids = dirty_ids
                query = query.filter(AdventurerS1ExpeditionLog.id.in_(dirty_ids))
            else:
                query = query.filter(
                    AdventurerS1ExpeditionLog.id > cursor,
                    AdventurerS1ExpeditionLog.id <= target,
                    AdventurerS1ExpeditionLog.id >= int(spec.get('continuity_min') or 0),
                ).order_by(AdventurerS1ExpeditionLog.id.asc()).limit(expedition_batch_size)
            rows = query.all()
            from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
                S1CompanionEvolutionStageRepairService,
            )
            from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
                S1CompanionHistoricalRepairService,
            )
            active_refs = S1CompanionHistoricalRepairService.active_reviewed_action_refs()
            events = []
            for row in rows:
                audit = S1CompanionEvolutionStageRepairService._build_expedition_continuity_audit(
                    [row],
                    resolved_action_refs=active_refs,
                )
                events.append({
                    'source_type': _EVENT_SOURCE_TYPES['continuity'],
                    'source_id': int(row.id),
                    'event_at': row.created_at,
                    'payload': audit,
                })

        detached = [{
            'source_type': str(event['source_type']),
            'source_id': int(event['source_id']),
            'event_at': event.get('event_at'),
            'payload': event.get('payload') or {},
        } for event in events]
        db_manager.rollback()
        next_cursor = (
            max((event['source_id'] for event in detached), default=cursor)
            if source != 'continuity_dirty' else cursor
        )
        if source != 'continuity_dirty' and next_cursor <= cursor:
            next_cursor = target
        return detached, next_cursor, processed_ids

    @classmethod
    def _persist_batch(
        cls,
        spec: dict,
        events: list[dict],
        next_cursor: int,
        processed_ids: list[int],
    ) -> Optional[dict]:
        source = str(spec['source'])
        persisted_source = (
            _EVENT_SOURCE_TYPES['continuity']
            if source == 'continuity_dirty' else _EVENT_SOURCE_TYPES[source]
        )
        event_ids = [int(event['source_id']) for event in events]
        with db_manager.transaction():
            cls._lock_scan_transaction()
            state = (
                db_manager.query(AdventurerS1CompanionEvolutionScanState)
                .filter_by(id=_SCAN_STATE_ID)
                .with_for_update().first()
            )
            if state is None or state.detection_mode != spec['detection_mode']:
                return None

            if source == 'continuity_dirty':
                current_dirty = {
                    int(log_id) for log_id in (state.dirty_expedition_log_ids_json or [])
                }
                if not set(processed_ids).issubset(current_dirty):
                    return None
                missing_ids = set(processed_ids) - set(event_ids)
                if missing_ids:
                    db_manager.query(AdventurerS1CompanionEvolutionScanEvent).filter(
                        AdventurerS1CompanionEvolutionScanEvent.source_type == persisted_source,
                        AdventurerS1CompanionEvolutionScanEvent.source_id.in_(missing_ids),
                    ).delete(synchronize_session=False)
                state.dirty_expedition_log_ids_json = sorted(
                    current_dirty - set(processed_ids)
                )
            else:
                cursors = dict(state.source_cursors_json or {})
                targets = dict(state.source_targets_json or {})
                if (
                    int(cursors.get(source) or 0) != int(spec.get('cursor') or 0)
                    or int(targets.get(source) or 0) != int(spec.get('target') or 0)
                ):
                    return None
                cursors[source] = int(next_cursor)
                state.source_cursors_json = cursors

            existing = {
                int(row.source_id): row
                for row in db_manager.query(AdventurerS1CompanionEvolutionScanEvent).filter(
                    AdventurerS1CompanionEvolutionScanEvent.source_type == persisted_source,
                    AdventurerS1CompanionEvolutionScanEvent.source_id.in_(event_ids),
                ).all()
            } if event_ids else {}
            new_rows = []
            for event in events:
                row = existing.get(int(event['source_id']))
                if row is None:
                    new_rows.append(AdventurerS1CompanionEvolutionScanEvent(
                        source_type=persisted_source,
                        source_id=int(event['source_id']),
                        event_at=event.get('event_at'),
                        payload_json=event.get('payload') or {},
                    ))
                else:
                    row.event_at = event.get('event_at')
                    row.payload_json = event.get('payload') or {}
            if new_rows:
                db_manager.add_all(new_rows)

            cursors = dict(state.source_cursors_json or {})
            targets = dict(state.source_targets_json or {})
            dirty_remaining = bool(state.dirty_expedition_log_ids_json)
            complete = (
                not dirty_remaining
                and all(
                    int(cursors.get(key) or 0) >= int(targets.get(key) or 0)
                    for key in _ORDERED_SOURCES
                )
            )
            if complete:
                state.status = 'finalizing'
            cached_events = db_manager.query(
                AdventurerS1CompanionEvolutionScanEvent,
            ).count()
            return cls._scan_payload(
                state,
                complete=False,
                source=source,
                processed_this_batch=len(events),
                cached_events=cached_events,
            )

    @staticmethod
    def _scan_payload(
        state: AdventurerS1CompanionEvolutionScanState,
        *,
        complete: bool,
        source: Optional[str] = None,
        processed_this_batch: int = 0,
        cached_events: int = 0,
    ) -> dict:
        cursors = {
            key: int(value or 0)
            for key, value in (state.source_cursors_json or {}).items()
            if key in _ORDERED_SOURCES
        }
        targets = {
            key: int(value or 0)
            for key, value in (state.source_targets_json or {}).items()
            if key in _ORDERED_SOURCES
        }
        return {
            'complete': bool(complete),
            'status': 'ready' if complete else str(state.status or 'scanning'),
            'mode': str((state.source_targets_json or {}).get('scan_mode') or 'incremental'),
            'source': source,
            'processed_this_batch': int(processed_this_batch),
            'cached_events': int(cached_events),
            'dirty_expedition_logs': len(state.dirty_expedition_log_ids_json or []),
            'cursors': cursors,
            'targets': targets,
            'progress_key': repr((state.status, cursors, targets, state.dirty_expedition_log_ids_json)),
        }

    @classmethod
    def _load_cached_inputs(cls) -> tuple[list[dict], list[dict], list[dict]]:
        evolution_logs: list[dict] = []
        transfer_events: list[dict] = []
        continuity_rows: list[dict] = []
        after_id = 0
        while True:
            rows = (
                db_manager.query(
                    AdventurerS1CompanionEvolutionScanEvent.id,
                    AdventurerS1CompanionEvolutionScanEvent.source_type,
                    AdventurerS1CompanionEvolutionScanEvent.source_id,
                    AdventurerS1CompanionEvolutionScanEvent.event_at,
                    AdventurerS1CompanionEvolutionScanEvent.payload_json,
                )
                .filter(AdventurerS1CompanionEvolutionScanEvent.id > after_id)
                .order_by(AdventurerS1CompanionEvolutionScanEvent.id.asc())
                .limit(500).all()
            )
            page = [{
                'id': int(row.id),
                'source_type': str(row.source_type),
                'source_id': int(row.source_id),
                'event_at': row.event_at,
                'payload': row.payload_json if isinstance(row.payload_json, dict) else {},
            } for row in rows]
            db_manager.rollback()
            if not page:
                break
            for row in page:
                payload = {**row['payload'], 'created_at': row['event_at']}
                if row['source_type'] == _EVENT_SOURCE_TYPES['evolution']:
                    evolution_logs.append(payload)
                elif row['source_type'] == _EVENT_SOURCE_TYPES['continuity']:
                    continuity_rows.append({
                        'source_id': row['source_id'],
                        'payload': row['payload'],
                    })
                else:
                    transfer_events.append(payload)
            after_id = page[-1]['id']
        evolution_logs.sort(key=lambda row: int(row['id']))
        return evolution_logs, transfer_events, continuity_rows

    @staticmethod
    def _aggregate_continuity(rows: list[dict], targets: dict) -> dict:
        from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
            _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT,
            _EXPEDITION_CONTINUITY_AUDIT_SAMPLE_LIMIT,
        )

        counters = (
            'reviewed_binding_count', 'risk_count', 'mapping_ambiguous_count',
            'stage_risk_count', 'type_risk_count', 'description_risk_count',
            'historical_review_receipt_count',
        )
        result = {key: 0 for key in counters}
        risk_samples: list[dict] = []
        for row in sorted(rows, key=lambda item: int(item['source_id']), reverse=True):
            payload = row['payload'] if isinstance(row.get('payload'), dict) else {}
            for key in counters:
                result[key] += int(payload.get(key) or 0)
            if len(risk_samples) < _EXPEDITION_CONTINUITY_AUDIT_SAMPLE_LIMIT:
                risk_samples.extend(
                    (payload.get('risk_samples') or [])[
                        :_EXPEDITION_CONTINUITY_AUDIT_SAMPLE_LIMIT - len(risk_samples)
                    ]
                )
        result.update({
            'scanned_logs': len(rows),
            'scan_limit': _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT,
            'scan_truncated': bool(targets.get('continuity_truncated')),
            'risk_samples': risk_samples,
            'risk_samples_truncated': result['risk_count'] > len(risk_samples),
            'calculation_basis': (
                '仅按冻结伙伴快照、最终结构化消耗/新增物品与已审核阶级绑定做机械核对；'
                '跨名实体归属和描述修复不自动推断。'
            ),
        })
        return result

    @classmethod
    def _finalize(cls) -> bool:
        from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
            S1CompanionEvolutionStageRepairService,
        )

        snapshot_state = db_manager.query(
            AdventurerS1CompanionEvolutionScanState,
        ).filter_by(id=_SCAN_STATE_ID).first()
        if (
            snapshot_state is None
            or snapshot_state.detection_mode != _DETECTION_MODE
            or snapshot_state.status != 'finalizing'
        ):
            db_manager.rollback()
            return False
        snapshot_cursors = dict(snapshot_state.source_cursors_json or {})
        snapshot_targets = dict(snapshot_state.source_targets_json or {})
        snapshot_revisions = dict(snapshot_state.source_revisions_json or {})
        snapshot_dirty_ids = list(snapshot_state.dirty_expedition_log_ids_json or [])
        db_manager.rollback()

        evolution_logs, transfer_events, continuity_rows = cls._load_cached_inputs()
        audited_stages = S1CompanionEvolutionStageRepairService._audited_stage_floors_by_usage()
        floors = S1CompanionEvolutionStageRepairService._build_successful_stage_floors(
            evolution_logs,
            transfer_events,
            audited_stages,
        )
        historical_usage_ids = set(
            S1CompanionEvolutionStageRepairService._fee_reconciliation_net_by_usage(None)
        )
        assessments, ambiguous = S1CompanionEvolutionStageRepairService._scan_fee_assessments(
            include_usage_log_ids=historical_usage_ids,
            evolution_logs=evolution_logs,
            transfer_events=transfer_events,
        )
        db_manager.rollback()

        with db_manager.transaction():
            cls._lock_scan_transaction()
            state = (
                db_manager.query(AdventurerS1CompanionEvolutionScanState)
                .filter_by(id=_SCAN_STATE_ID)
                .with_for_update().first()
            )
            if (
                state is None
                or state.detection_mode != _DETECTION_MODE
                or state.status != 'finalizing'
                or dict(state.source_cursors_json or {}) != snapshot_cursors
                or dict(state.source_targets_json or {}) != snapshot_targets
                or dict(state.source_revisions_json or {}) != snapshot_revisions
                or list(state.dirty_expedition_log_ids_json or []) != snapshot_dirty_ids
            ):
                return False
            live = cls._live_snapshot()
            if (
                live['targets'] != {
                    key: value for key, value in (state.source_targets_json or {}).items()
                    if key != 'scan_mode'
                }
                or live['revisions'] != (state.source_revisions_json or {})
                or state.dirty_expedition_log_ids_json
            ):
                state.status = 'ready'
                cls._reconcile_ready_state(state, live)
                return False

            targets = dict(state.source_targets_json or {})
            state.cached_result_json = {
                'stage_floors': [
                    {
                        'adventurer_id': int(key[0]),
                        'item_name': str(key[1]),
                        **value,
                    }
                    for key, value in floors.items()
                ],
                'fee_assessments': assessments,
                'fee_ambiguous': int(ambiguous),
                'continuity_audit': cls._aggregate_continuity(
                    continuity_rows, targets,
                ),
            }
            state.status = 'ready'
            return True

    @classmethod
    def ready_artifacts(cls, *, require_current: bool = False) -> Optional[dict]:
        if require_current:
            has_ready_state = db_manager.query(
                AdventurerS1CompanionEvolutionScanState.id,
            ).filter_by(
                id=_SCAN_STATE_ID,
                detection_mode=_DETECTION_MODE,
                status='ready',
            ).first() is not None
            db_manager.rollback()
            if not has_ready_state:
                return None
            prepared = cls._prepare()
            if prepared.get('action') != 'ready':
                return None
        state = db_manager.query(AdventurerS1CompanionEvolutionScanState).filter_by(
            id=_SCAN_STATE_ID,
            detection_mode=_DETECTION_MODE,
            status='ready',
        ).first()
        result = (
            dict(state.cached_result_json)
            if state is not None and isinstance(state.cached_result_json, dict)
            else None
        )
        db_manager.rollback()
        return result

    @classmethod
    def preview_integrity(cls, admin_user_id: int):
        try:
            for _attempt in range(3):
                prepared = cls._prepare()
                if prepared['action'] == 'ready':
                    artifacts = cls.ready_artifacts()
                    if artifacts is None:
                        continue
                    from plugins.adventurer_s1.services.s1_companion_evolution_stage_repair_service import (
                        S1CompanionEvolutionStageRepairService,
                    )
                    fee, fee_err = (
                        S1CompanionEvolutionStageRepairService.preview_fee_reconciliation(
                            admin_user_id,
                            _assessments=list(artifacts.get('fee_assessments') or []),
                            _ambiguous=int(artifacts.get('fee_ambiguous') or 0),
                        )
                    )
                    if fee_err:
                        return None, fee_err
                    stage, stage_err = S1CompanionEvolutionStageRepairService.preview_from_artifacts(
                        list(artifacts.get('stage_floors') or []),
                        dict(artifacts.get('continuity_audit') or {}),
                    )
                    if stage_err:
                        return None, stage_err
                    state = db_manager.query(
                        AdventurerS1CompanionEvolutionScanState,
                    ).filter_by(id=_SCAN_STATE_ID).first()
                    cached_events = db_manager.query(
                        AdventurerS1CompanionEvolutionScanEvent,
                    ).count()
                    scan = cls._scan_payload(
                        state,
                        complete=True,
                        cached_events=cached_events,
                    )
                    db_manager.rollback()
                    return {'scan': scan, 'fee': fee, 'stage': stage}, None
                if prepared['action'] == 'finalize':
                    if cls._finalize():
                        continue
                    continue

                events, next_cursor, processed_ids = cls._read_batch(prepared)
                scan = cls._persist_batch(
                    prepared, events, next_cursor, processed_ids,
                )
                if scan is not None:
                    return {'scan': scan}, None
            return None, '扫描水位持续变化，请稍后继续预览'
        except Exception as exc:  # noqa: BLE001
            db_manager.rollback()
            current_app.logger.exception('S1 companion evolution incremental preview failed')
            return None, f'分批预览失败：{exc}'

    @classmethod
    def mark_derived_dirty(cls) -> None:
        try:
            with db_manager.transaction():
                cls._lock_scan_transaction()
                state = (
                    db_manager.query(AdventurerS1CompanionEvolutionScanState)
                    .filter_by(id=_SCAN_STATE_ID).with_for_update().first()
                )
                if state is not None and state.status == 'ready':
                    state.status = 'finalizing'
                    state.cached_result_json = None
        except Exception:  # noqa: BLE001
            db_manager.rollback()
            current_app.logger.exception('Failed to invalidate companion evolution derived scan')

    @classmethod
    def mark_continuity_logs_dirty(cls, expedition_log_ids: set[int]) -> None:
        normalized = {int(log_id) for log_id in expedition_log_ids if int(log_id) > 0}
        if not normalized:
            return
        try:
            with db_manager.transaction():
                cls._lock_scan_transaction()
                state = (
                    db_manager.query(AdventurerS1CompanionEvolutionScanState)
                    .filter_by(id=_SCAN_STATE_ID).with_for_update().first()
                )
                if state is None:
                    return
                targets = dict(state.source_targets_json or {})
                minimum = int(targets.get('continuity_min') or 0)
                maximum = int(targets.get('continuity') or 0)
                in_window = {
                    log_id for log_id in normalized
                    if minimum <= log_id <= maximum
                }
                if not in_window:
                    return
                dirty = {
                    int(log_id) for log_id in (state.dirty_expedition_log_ids_json or [])
                }
                state.dirty_expedition_log_ids_json = sorted(dirty | in_window)
                state.status = 'scanning'
                state.cached_result_json = None
        except Exception:  # noqa: BLE001
            db_manager.rollback()
            current_app.logger.exception('Failed to invalidate companion continuity scan rows')


__all__ = ['S1CompanionEvolutionIntegrityScanService']
