"""Repair companion evolution stages lost by the historical warehouse move bug."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Iterable, Iterator, Optional, Tuple

from flask import current_app, has_app_context

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BugReclaimBatch,
    AdventurerS1BugReclaimItem,
    AdventurerS1BuildingUsageLog,
    AdventurerS1CurrencyLog,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1MarketBuyOffer,
    AdventurerS1MarketBuyOrder,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)


_BACKUP_KEY = 'companion_evolution_stage_repair_backup_v1'
_FEE_RECLAIM_OPERATION = 'companion_evolution_fee_reclaims'
_FEE_RECLAIM_ISSUE_PREFIX = 'companion_evolution_fee_evasion:v1:'
_FEE_RECONCILE_OPERATION = 'companion_evolution_fee_reconciliation'
_FEE_RECONCILE_ISSUE_PREFIX = 'companion_evolution_fee_reconciliation:v1:'
_REVIEWED_EXPEDITION_COMPANION_FLOORS_KEY = '_reviewed_companion_stage_floors'
_EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT = 5000
_EXPEDITION_CONTINUITY_AUDIT_SAMPLE_LIMIT = 50
_CONTAINER_MODELS = (
    ('equipment', AdventurerS1Equipment),
    ('warehouse', AdventurerS1WarehouseItem),
)


class S1CompanionEvolutionStageRepairService:
    """Use successful altar logs as a monotonic floor for current companion instances."""

    @staticmethod
    def _companion_transfer_events() -> list[dict]:
        """Return authoritative completed market transfers with persisted companion stages."""
        events: list[dict] = []

        after_id = 0
        while True:
            rows = (
                AdventurerS1MarketListing.query
                .with_entities(
                    AdventurerS1MarketListing.id,
                    AdventurerS1MarketListing.seller_adventurer_id,
                    AdventurerS1MarketListing.buyer_adventurer_id,
                    AdventurerS1MarketListing.item_name,
                    AdventurerS1MarketListing.evolution_stage,
                    AdventurerS1MarketListing.completed_at,
                )
                .filter(
                    AdventurerS1MarketListing.id > after_id,
                    AdventurerS1MarketListing.status == 'completed',
                    AdventurerS1MarketListing.item_type == 'companion',
                    AdventurerS1MarketListing.buyer_adventurer_id.isnot(None),
                    AdventurerS1MarketListing.evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketListing.id.asc())
                .limit(500)
                .all()
            )
            db_manager.rollback()
            if not rows:
                break
            events.extend({
                'event_type': 'market_listing_transfer',
                'event_id': int(row.id),
                'from_adventurer_id': int(row.seller_adventurer_id),
                'to_adventurer_id': int(row.buyer_adventurer_id),
                'item_name': str(row.item_name),
                'stage': max(0, int(row.evolution_stage or 0)),
                'created_at': row.completed_at,
            } for row in rows)
            after_id = int(rows[-1][0])

        after_id = 0
        while True:
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
                    AdventurerS1MarketOffer.id > after_id,
                    AdventurerS1MarketOffer.status == 'accepted',
                    AdventurerS1MarketOffer.offered_item_type == 'companion',
                    AdventurerS1MarketOffer.offered_evolution_stage.isnot(None),
                    AdventurerS1MarketOffer.offered_evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketOffer.id.asc())
                .limit(500)
                .all()
            )
            db_manager.rollback()
            if not rows:
                break
            events.extend({
                'event_type': 'market_barter_offer_transfer',
                'event_id': int(row.id),
                'from_adventurer_id': int(row.buyer_adventurer_id),
                'to_adventurer_id': int(row.seller_adventurer_id),
                'item_name': str(row.offered_item_name),
                'stage': max(0, int(row.offered_evolution_stage or 0)),
                'created_at': row.updated_at,
            } for row in rows)
            after_id = int(rows[-1][0])

        after_id = 0
        while True:
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
                    AdventurerS1MarketBuyOffer.id > after_id,
                    AdventurerS1MarketBuyOffer.status == 'accepted',
                    AdventurerS1MarketBuyOffer.item_type == 'companion',
                    AdventurerS1MarketBuyOffer.evolution_stage.isnot(None),
                    AdventurerS1MarketBuyOffer.evolution_stage > 0,
                )
                .order_by(AdventurerS1MarketBuyOffer.id.asc())
                .limit(500)
                .all()
            )
            db_manager.rollback()
            if not rows:
                break
            events.extend({
                'event_type': 'market_buy_offer_transfer',
                'event_id': int(row.id),
                'from_adventurer_id': int(row.seller_adventurer_id),
                'to_adventurer_id': int(row.buyer_adventurer_id),
                'item_name': str(row.item_name),
                'stage': max(0, int(row.evolution_stage or 0)),
                'created_at': row.updated_at,
            } for row in rows)
            after_id = int(rows[-1][0])

        return events

    @staticmethod
    def _iter_successful_evolution_logs() -> Iterator[dict]:
        """Yield successful evolution logs in creation order using bounded pages."""
        after_id = 0
        while True:
            rows = (
                AdventurerS1BuildingUsageLog.query
                .with_entities(
                    AdventurerS1BuildingUsageLog.id,
                    AdventurerS1BuildingUsageLog.adventurer_id,
                    AdventurerS1BuildingUsageLog.building_instance_id,
                    AdventurerS1BuildingUsageLog.result,
                    AdventurerS1BuildingUsageLog.created_at,
                )
                .filter(
                    AdventurerS1BuildingUsageLog.id > after_id,
                    AdventurerS1BuildingUsageLog.result['type'].as_string() == 'pet_evolution',
                    AdventurerS1BuildingUsageLog.result['success'].as_boolean().is_(True),
                )
                .order_by(AdventurerS1BuildingUsageLog.id.asc())
                .limit(500).all()
            )
            page = [{
                'id': int(log_id),
                'adventurer_id': int(adventurer_id),
                'building_instance_id': int(building_instance_id),
                'result': result,
                'created_at': created_at,
            } for log_id, adventurer_id, building_instance_id, result, created_at in rows]
            db_manager.rollback()
            if not page:
                break
            yield from page
            after_id = int(page[-1]['id'])

    @staticmethod
    def _build_successful_stage_floors(
        evolution_logs: Iterable[dict],
        transfer_events: list[dict],
        audited_stages: Optional[dict[tuple[int, int], int]] = None,
    ) -> dict[tuple[int, str], dict]:
        """Reconstruct terminal companion stages from unique, owner-scoped lineages.

        A successful evolution may rename the companion while a historical bug resets
        the persisted snapshot to stage zero. Propagate the unique predecessor's stage
        through ``old_name -> new_name`` instead of trusting each renamed form's
        recorded stage in isolation. Keys always include the persisted adventurer id,
        so archived/dead and later live adventurers owned by one user cannot mix.
        """
        audited_stages = audited_stages or {}
        transfers: list[dict] = []
        for transfer in transfer_events:
            try:
                event_id = int(transfer['event_id'])
                to_adventurer_id = int(transfer['to_adventurer_id'])
            except (KeyError, TypeError, ValueError):
                continue
            transfers.append({
                **transfer,
                'event_id': event_id,
                'to_adventurer_id': to_adventurer_id,
            })
        transfers.sort(key=lambda event: (
            event.get('created_at') is None,
            event.get('created_at') or datetime.max,
            int(event['event_id']),
        ))

        producers: dict[tuple[int, str], list[dict]] = {}

        def pop_candidate(
            key: tuple[int, str],
            candidate_index: int,
        ) -> dict:
            candidates = producers[key]
            producer = candidates.pop(candidate_index)
            if not candidates:
                producers.pop(key, None)
            return producer

        def consume_predecessor(
            key: tuple[int, str],
            *,
            companion_id: Optional[int],
            before_snapshot: dict,
            audited_from_stage: Optional[int],
        ) -> Optional[dict]:
            candidates = producers.get(key) or []
            if not candidates:
                return None

            if companion_id is not None:
                exact_ids = [
                    index for index, candidate in enumerate(candidates)
                    if candidate.get('companion_id') == companion_id
                ]
                if len(exact_ids) == 1:
                    return pop_candidate(key, exact_ids[0])

            before_desc = str(before_snapshot.get('custom_desc') or '').strip()
            if before_desc:
                exact_snapshots = [
                    index for index, candidate in enumerate(candidates)
                    if candidate.get('after_name') == key[1]
                    and candidate.get('after_custom_desc') == before_desc
                ]
                if len(exact_snapshots) == 1:
                    return pop_candidate(key, exact_snapshots[0])

            if len(candidates) == 1 and audited_from_stage is not None:
                # Historical equipment <-> warehouse moves recreated the physical
                # row and therefore changed companion_id. An executed fee audit is
                # bound to this exact adventurer + usage log and records the
                # authoritative pre-evolution stage. Combine that evidence with a
                # unique predecessor name instead of relaxing to name-only matching.
                candidate_stage = max(
                    0, int(candidates[0].get('stage') or 0),
                )
                if int(audited_from_stage) >= candidate_stage:
                    return pop_candidate(key, 0)

            if len(candidates) == 1 and candidates[0].get('explicit_transfer'):
                return pop_candidate(key, 0)
            if (
                len(candidates) == 1
                and companion_id is None
                and candidates[0].get('companion_id') is None
            ):
                # Old logs predating instance ids retain the historical unique-name
                # fallback. Once either side has an id, an id mismatch is ambiguous
                # unless a snapshot or explicit market transfer proves continuity.
                return pop_candidate(key, 0)
            return None

        def apply_transfer(event: dict) -> None:
            name = str(event.get('item_name') or '').strip()
            try:
                stage = max(0, int(event.get('stage') or 0))
            except (TypeError, ValueError):
                return
            if not name or stage <= 0:
                return
            try:
                from_adventurer_id = int(event['from_adventurer_id'])
            except (KeyError, TypeError, ValueError):
                from_adventurer_id = None
            if from_adventurer_id is not None:
                source_key = (from_adventurer_id, name)
                source_candidates = producers.get(source_key) or []
                if len(source_candidates) == 1:
                    pop_candidate(source_key, 0)
            key = (int(event['to_adventurer_id']), name)
            producers.setdefault(key, []).append({
                'stage': stage,
                'transfer_type': str(event.get('event_type') or 'market_transfer'),
                'transfer_id': int(event['event_id']),
                'explicit_transfer': True,
                'companion_id': None,
                'after_name': name,
                'after_custom_desc': None,
            })

        def apply_evolution(event: dict) -> None:
            result = event.get('result')
            if not isinstance(result, dict):
                return
            new_name = str(result.get('new_name') or '').strip()
            if not new_name:
                return
            try:
                recorded_stage = max(0, int(result.get('evolution_stage') or 0))
            except (TypeError, ValueError):
                return

            adventurer_id = int(event['adventurer_id'])
            old_name = str(result.get('old_name') or '').strip()
            before = (
                result.get('pre_evolution_snapshot')
                if isinstance(result.get('pre_evolution_snapshot'), dict)
                else {}
            )
            try:
                recorded_from_stage = max(
                    0, int(before.get('evolution_stage') or 0),
                )
            except (TypeError, ValueError):
                recorded_from_stage = max(0, recorded_stage - 1)
            try:
                companion_id = int(result['companion_id'])
            except (KeyError, TypeError, ValueError):
                companion_id = None
            audited_from_stage = audited_stages.get((
                adventurer_id,
                int(event['id']),
            ))

            inherited_stage = recorded_from_stage
            if old_name:
                predecessor = consume_predecessor(
                    (adventurer_id, old_name),
                    companion_id=companion_id,
                    before_snapshot=before,
                    audited_from_stage=audited_from_stage,
                )
                if predecessor is not None:
                    inherited_stage = max(
                        inherited_stage,
                        int(predecessor.get('stage') or 0),
                        int(audited_from_stage or 0),
                    )
                stage_after = max(recorded_stage, inherited_stage + 1)
            else:
                # Legacy logs without old_name cannot prove a rename edge. Preserve
                # their direct recorded floor without inventing an extra stage.
                stage_after = recorded_stage
            if stage_after <= 0:
                return
            undo = result.get('_undo_snapshot')
            after = (
                undo.get('after')
                if isinstance(undo, dict) and isinstance(undo.get('after'), dict)
                else {}
            )
            producers.setdefault((adventurer_id, new_name), []).append({
                'stage': stage_after,
                'log_id': int(event['id']),
                'companion_id': companion_id,
                'after_name': str(after.get('name') or new_name).strip(),
                'after_custom_desc': str(after.get('custom_desc') or '').strip() or None,
                'explicit_transfer': False,
            })

        transfer_index = 0
        for raw_log in evolution_logs:
            try:
                log = {
                    **raw_log,
                    'id': int(raw_log['id']),
                    'adventurer_id': int(raw_log['adventurer_id']),
                }
            except (KeyError, TypeError, ValueError):
                continue
            log_time = log.get('created_at')
            while transfer_index < len(transfers):
                transfer_time = transfers[transfer_index].get('created_at')
                if transfer_time is None:
                    break
                if log_time is not None and transfer_time > log_time:
                    break
                apply_transfer(transfers[transfer_index])
                transfer_index += 1
            apply_evolution(log)
        for transfer in transfers[transfer_index:]:
            apply_transfer(transfer)

        floors: dict[tuple[int, str], dict] = {}
        for key, candidates in producers.items():
            # Multiple terminal producers with the same owner and name cannot be
            # matched safely to one current instance. _scan also guards duplicate
            # current rows; this guard covers ambiguity in the historical lineage.
            if len(candidates) == 1:
                candidate = candidates[0]
                floor = {'stage': int(candidate['stage'])}
                for evidence_key in (
                    'log_id', 'transfer_type', 'transfer_id',
                ):
                    if candidate.get(evidence_key) is not None:
                        floor[evidence_key] = candidate[evidence_key]
                floors[key] = floor
        return floors

    @staticmethod
    def _audited_stage_floors_by_usage() -> dict[tuple[int, int], int]:
        """Load executed fee-audit pre-stage floors keyed by exact adventurer/log."""
        floors: dict[tuple[int, int], int] = {}
        after_id = 0
        while True:
            rows = (
                db_manager.query(
                    AdventurerS1BugReclaimItem.id,
                    AdventurerS1BugReclaimItem.adventurer_id,
                    AdventurerS1BugReclaimItem.issue_key,
                    AdventurerS1BugReclaimItem.result_json,
                    AdventurerS1BugReclaimItem.after_json,
                )
                .filter(
                    AdventurerS1BugReclaimItem.id > after_id,
                    AdventurerS1BugReclaimItem.issue_type.in_((
                        'companion_evolution_fee_evasion',
                        'companion_evolution_fee_reconciliation',
                    )),
                    AdventurerS1BugReclaimItem.status == 'executed',
                    AdventurerS1BugReclaimItem.reverted_at.is_(None),
                )
                .order_by(AdventurerS1BugReclaimItem.id.asc())
                .limit(500)
                .all()
            )
            page = list(rows)
            db_manager.rollback()
            if not page:
                break
            for row in page:
                usage_log_id = (
                    S1CompanionEvolutionStageRepairService._fee_usage_log_id(row)
                )
                after = row.after_json if isinstance(row.after_json, dict) else {}
                result = row.result_json if isinstance(row.result_json, dict) else {}
                try:
                    adventurer_id = int(row.adventurer_id)
                    expected_stage = max(
                        0,
                        int(
                            after.get('expected_stage')
                            if after.get('expected_stage') is not None
                            else result.get('expected_stage')
                        ),
                    )
                except (TypeError, ValueError):
                    continue
                if usage_log_id is None:
                    continue
                key = (adventurer_id, int(usage_log_id))
                floors[key] = max(floors.get(key, 0), expected_stage)
            after_id = int(page[-1].id)
        return floors

    @classmethod
    def _successful_stage_floors(cls) -> dict[tuple[int, str], dict]:
        if has_app_context():
            from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
                S1CompanionEvolutionIntegrityScanService,
            )
            artifacts = S1CompanionEvolutionIntegrityScanService.ready_artifacts(
                require_current=True,
            )
            if artifacts is not None:
                return cls._stage_floor_map(artifacts.get('stage_floors') or [])
        return cls._build_successful_stage_floors(
            cls._iter_successful_evolution_logs(),
            cls._companion_transfer_events(),
            cls._audited_stage_floors_by_usage(),
        )

    @staticmethod
    def _stage_floor_map(rows: Iterable[dict]) -> dict[tuple[int, str], dict]:
        floors: dict[tuple[int, str], dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                adventurer_id = int(row['adventurer_id'])
                item_name = str(row['item_name']).strip()
                stage = max(0, int(row['stage']))
            except (KeyError, TypeError, ValueError):
                continue
            if adventurer_id <= 0 or not item_name or stage <= 0:
                continue
            floor = {'stage': stage}
            for key in ('log_id', 'transfer_type', 'transfer_id'):
                if row.get(key) is not None:
                    floor[key] = row[key]
            floors[(adventurer_id, item_name)] = floor
        return floors

    @staticmethod
    def _load_current_companions(adventurer_ids: set[int], *, lock: bool = False) -> list[tuple[str, object]]:
        if not adventurer_ids:
            return []
        rows: list[tuple[str, object]] = []
        sorted_ids = sorted(adventurer_ids)
        for source, model in _CONTAINER_MODELS:
            for start in range(0, len(sorted_ids), 500):
                query = model.query.filter(
                    model.adventurer_id.in_(sorted_ids[start:start + 500]),
                    model.type == 'companion',
                ).order_by(model.id.asc())
                if lock:
                    query = query.with_for_update()
                rows.extend((source, row) for row in query.all())
        for start in range(0, len(sorted_ids), 500):
            query = AdventurerS1MarketListing.query.filter(
                AdventurerS1MarketListing.seller_adventurer_id.in_(
                    sorted_ids[start:start + 500],
                ),
                AdventurerS1MarketListing.item_type == 'companion',
                AdventurerS1MarketListing.status == 'active',
            ).order_by(AdventurerS1MarketListing.id.asc())
            if lock:
                query = query.with_for_update()
            for row in query.all():
                # Normalize escrow field names for the shared repair matcher.
                row.adventurer_id = row.seller_adventurer_id
                row.name = row.item_name
                row.type = row.item_type
                rows.append(('market_listing', row))
        return rows

    @staticmethod
    def _audit_text(value: object) -> str:
        return str(value or '').strip()

    @staticmethod
    def _audit_nonnegative_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _build_expedition_continuity_audit(
        cls,
        logs: Iterable[object],
        *,
        scan_truncated: bool = False,
        resolved_action_refs: Optional[set[str]] = None,
    ) -> dict:
        """Report historical companion replacements that lack a safe binding.

        This is deliberately report-only. A same-round consume/gain pair is not
        enough evidence to decide that two differently named items are one entity.
        """
        risk_count = 0
        reviewed_binding_count = 0
        mapping_ambiguous_count = 0
        stage_risk_count = 0
        type_risk_count = 0
        description_risk_count = 0
        historical_review_receipt_count = 0
        risk_samples: list[dict] = []
        scanned_logs = 0
        resolved_action_refs = resolved_action_refs or set()

        for log in logs:
            scanned_logs += 1
            full_log = (
                log.full_log
                if isinstance(getattr(log, 'full_log', None), dict)
                else {}
            )
            snapshots_by_id: dict[int, list[dict]] = {}
            for snapshot in full_log.get('audit_member_snapshot') or []:
                if not isinstance(snapshot, dict):
                    continue
                try:
                    adventurer_id = int(snapshot.get('adventurer_id'))
                except (TypeError, ValueError):
                    continue
                snapshots_by_id.setdefault(adventurer_id, []).append(snapshot)

            bindings_by_key: dict[tuple[int, str], list[dict]] = {}
            for binding in full_log.get(
                _REVIEWED_EXPEDITION_COMPANION_FLOORS_KEY,
            ) or []:
                if not isinstance(binding, dict):
                    continue
                try:
                    adventurer_id = int(binding.get('adventurer_id'))
                except (TypeError, ValueError):
                    continue
                consumed_name = cls._audit_text(binding.get('consumed_name'))
                surviving_name = cls._audit_text(binding.get('surviving_name'))
                if adventurer_id <= 0 or not consumed_name or not surviving_name:
                    continue
                bindings_by_key.setdefault(
                    (adventurer_id, consumed_name),
                    [],
                ).append({
                    'surviving_name': surviving_name,
                    'stage_floor': cls._audit_nonnegative_int(
                        binding.get('stage_floor'),
                    ),
                })

            for update in full_log.get('member_updates') or []:
                if not isinstance(update, dict):
                    continue
                try:
                    adventurer_id = int(update.get('id'))
                except (TypeError, ValueError):
                    continue
                snapshots = snapshots_by_id.get(adventurer_id) or []
                if len(snapshots) != 1:
                    continue
                snapshot = snapshots[0]

                new_payload = update.get('new_equipments')
                if not isinstance(new_payload, dict) or not new_payload:
                    continue
                new_items: list[dict] = []
                new_items_by_name: dict[str, dict] = {}
                for raw_name, raw_metadata in new_payload.items():
                    name = cls._audit_text(raw_name)
                    if not name:
                        continue
                    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                    description = cls._audit_text(
                        metadata.get('desc') or metadata.get('description'),
                    )
                    item = {
                        'name': name,
                        'type': cls._audit_text(metadata.get('type')),
                        'evolution_stage': cls._audit_nonnegative_int(
                            metadata.get('evolution_stage'),
                        ),
                        'description': description,
                    }
                    new_items.append(item)
                    new_items_by_name[name] = item
                if not new_items:
                    continue

                consumed_quantities: dict[str, int] = {}
                raw_consumed = update.get('consumed_equipments')
                consumed_rows = (
                    raw_consumed if isinstance(raw_consumed, list)
                    else [raw_consumed]
                )
                for consumed in consumed_rows:
                    if isinstance(consumed, dict):
                        name = cls._audit_text(consumed.get('name'))
                        quantity = max(
                            1,
                            cls._audit_nonnegative_int(
                                consumed.get('quantity'),
                            ),
                        )
                    elif isinstance(consumed, str):
                        name = cls._audit_text(consumed)
                        quantity = 1
                    else:
                        continue
                    if name:
                        consumed_quantities[name] = (
                            consumed_quantities.get(name, 0) + quantity
                        )

                companion_rows_by_name: dict[str, list[dict]] = {}
                available_by_name: dict[str, int] = {}
                for equipment in snapshot.get('equipment') or []:
                    if not isinstance(equipment, dict):
                        continue
                    if cls._audit_text(equipment.get('type')) != 'companion':
                        continue
                    name = cls._audit_text(equipment.get('name'))
                    if not name:
                        continue
                    quantity = max(
                        1,
                        cls._audit_nonnegative_int(
                            equipment.get('quantity'),
                        ),
                    )
                    uses_left = equipment.get('uses_left')
                    available_units = quantity
                    if quantity == 1 and uses_left is not None:
                        available_units = max(
                            1,
                            cls._audit_nonnegative_int(uses_left),
                        )
                    companion_rows_by_name.setdefault(name, []).append(equipment)
                    available_by_name[name] = (
                        available_by_name.get(name, 0) + available_units
                    )

                for consumed_name, consumed_quantity in consumed_quantities.items():
                    departure_rows = companion_rows_by_name.get(consumed_name) or []
                    available_quantity = available_by_name.get(consumed_name)
                    if (
                        not departure_rows
                        or available_quantity is None
                        or consumed_quantity < available_quantity
                    ):
                        continue

                    departure_stage = max(
                        cls._audit_nonnegative_int(
                            row.get('evolution_stage'),
                        )
                        for row in departure_rows
                    )
                    departure_descriptions = {
                        cls._audit_text(
                            row.get('description') or row.get('desc'),
                        )
                        for row in departure_rows
                        if cls._audit_text(
                            row.get('description') or row.get('desc'),
                        )
                    }
                    frozen_description = (
                        next(iter(departure_descriptions))
                        if len(departure_descriptions) == 1
                        else ''
                    )

                    from plugins.adventurer_s1.services.s1_companion_historical_review_contract import (
                        stable_ref,
                    )
                    action_ref = stable_ref(
                        'cmp',
                        int(getattr(log, 'id')),
                        adventurer_id,
                        consumed_name,
                    )
                    if action_ref in resolved_action_refs:
                        historical_review_receipt_count += 1
                        continue

                    bindings = bindings_by_key.get(
                        (adventurer_id, consumed_name),
                    ) or []
                    risk_codes: list[str] = []
                    reviewed_surviving_name = ''
                    candidate_new_item: Optional[dict] = None
                    if len(bindings) == 1:
                        reviewed_binding_count += 1
                        reviewed_surviving_name = bindings[0]['surviving_name']
                        candidate_new_item = new_items_by_name.get(
                            reviewed_surviving_name,
                        )
                        if candidate_new_item is None:
                            risk_codes.append('reviewed_survivor_missing')
                        else:
                            if candidate_new_item['type'] != 'companion':
                                risk_codes.append(
                                    'reviewed_survivor_type_mismatch',
                                )
                            if (
                                candidate_new_item['evolution_stage']
                                < max(
                                    departure_stage,
                                    bindings[0]['stage_floor'],
                                )
                            ):
                                risk_codes.append(
                                    'reviewed_survivor_stage_regression',
                                )
                            if (
                                frozen_description
                                and frozen_description
                                not in candidate_new_item['description']
                            ):
                                risk_codes.append(
                                    'reviewed_survivor_description_not_verbatim_preserved',
                                )
                    else:
                        risk_codes.append(
                            'continuity_binding_missing'
                            if not bindings
                            else 'continuity_binding_ambiguous'
                        )
                        diagnostic_candidates = (
                            new_items
                            if len(new_items) == 1
                            else [
                                item for item in new_items
                                if item['type'] == 'companion'
                            ]
                        )
                        if len(diagnostic_candidates) == 1:
                            candidate_new_item = diagnostic_candidates[0]
                            if candidate_new_item['type'] != 'companion':
                                risk_codes.append(
                                    'single_new_item_not_companion',
                                )
                            if (
                                candidate_new_item['type'] == 'companion'
                                and candidate_new_item['evolution_stage']
                                < departure_stage
                            ):
                                risk_codes.append(
                                    'single_new_companion_stage_below_departure',
                                )
                            if (
                                frozen_description
                                and frozen_description
                                not in candidate_new_item['description']
                            ):
                                risk_codes.append(
                                    'single_new_item_missing_frozen_description',
                                )
                        else:
                            risk_codes.append('new_item_mapping_ambiguous')

                    if not risk_codes:
                        continue
                    risk_count += 1
                    if any('ambiguous' in code for code in risk_codes):
                        mapping_ambiguous_count += 1
                    if any('stage' in code for code in risk_codes):
                        stage_risk_count += 1
                    if any('type' in code or 'not_companion' in code for code in risk_codes):
                        type_risk_count += 1
                    if any('description' in code for code in risk_codes):
                        description_risk_count += 1
                    if len(risk_samples) >= _EXPEDITION_CONTINUITY_AUDIT_SAMPLE_LIMIT:
                        continue
                    created_at = getattr(log, 'created_at', None)
                    risk_samples.append({
                        'action_ref': action_ref,
                        'expedition_log_id': int(getattr(log, 'id')),
                        'party_name': cls._audit_text(
                            getattr(log, 'party_name', ''),
                        ),
                        'created_at': (
                            created_at.isoformat()
                            if created_at is not None else None
                        ),
                        'adventurer_id': adventurer_id,
                        'adventurer_name': cls._audit_text(
                            snapshot.get('adventurer_name'),
                        ),
                        'consumed_name': consumed_name,
                        'consumed_quantity': consumed_quantity,
                        'departure_instance_count': len(departure_rows),
                        'departure_stage': departure_stage,
                        'departure_description_length': len(
                            frozen_description,
                        ),
                        'reviewed_surviving_name': (
                            reviewed_surviving_name or None
                        ),
                        'new_item_count': len(new_items),
                        'new_items': [{
                            'name': item['name'],
                            'type': item['type'],
                            'evolution_stage': item['evolution_stage'],
                            'description_length': len(item['description']),
                        } for item in new_items[:20]],
                        'new_items_truncated': len(new_items) > 20,
                        'risk_codes': risk_codes,
                        'auto_action': 'none',
                    })

        return {
            'scanned_logs': scanned_logs,
            'scan_limit': _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT,
            'scan_truncated': bool(scan_truncated),
            'reviewed_binding_count': reviewed_binding_count,
            'risk_count': risk_count,
            'mapping_ambiguous_count': mapping_ambiguous_count,
            'stage_risk_count': stage_risk_count,
            'type_risk_count': type_risk_count,
            'description_risk_count': description_risk_count,
            'historical_review_receipt_count': historical_review_receipt_count,
            'risk_samples': risk_samples,
            'risk_samples_truncated': (
                risk_count > len(risk_samples)
            ),
            'calculation_basis': (
                '仅按冻结伙伴快照、最终结构化消耗/新增物品与已审核阶级绑定做机械核对；'
                '跨名实体归属和描述修复不自动推断。'
            ),
        }

    @classmethod
    def _scan_expedition_continuity_audit(
        cls,
        *,
        expedition_log_id: Optional[int] = None,
    ) -> dict:
        query = (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.party_name,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.created_at,
            )
            .filter(AdventurerS1ExpeditionLog.status == 'completed')
        )
        if expedition_log_id is not None:
            query = query.filter(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
            )
            rows = query.all()
            scan_truncated = False
            selected_rows = rows
        else:
            rows = (
                query
                .order_by(AdventurerS1ExpeditionLog.id.desc())
                .limit(_EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT + 1)
                .all()
            )
            scan_truncated = (
                len(rows) > _EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT
            )
            selected_rows = rows[:_EXPEDITION_CONTINUITY_AUDIT_LOG_LIMIT]
        from plugins.adventurer_s1.services.s1_companion_historical_repair_service import (
            S1CompanionHistoricalRepairService,
        )
        audit = cls._build_expedition_continuity_audit(
            selected_rows,
            scan_truncated=scan_truncated,
            resolved_action_refs=(
                S1CompanionHistoricalRepairService.active_reviewed_action_refs()
            ),
        )
        db_manager.rollback()
        return audit

    @classmethod
    def _scan(
        cls,
        floors: Optional[dict[tuple[int, str], dict]] = None,
    ) -> tuple[list[dict], int]:
        floors = floors if floors is not None else cls._successful_stage_floors()
        if not floors:
            return [], 0
        current_by_key: dict[tuple[int, str], list[tuple[str, object]]] = {}
        current_rows = [
            (source, SimpleNamespace(
                id=int(row.id),
                adventurer_id=int(row.adventurer_id),
                name=str(row.name),
                evolution_stage=int(row.evolution_stage or 0),
            ))
            for source, row in cls._load_current_companions({key[0] for key in floors})
        ]
        # Keep candidate grouping and sorting outside SQLAlchemy's implicit transaction.
        db_manager.rollback()
        for source, row in current_rows:
            current_by_key.setdefault((int(row.adventurer_id), str(row.name)), []).append((source, row))

        candidates: list[dict] = []
        ambiguous = 0
        for key, floor in floors.items():
            matches = current_by_key.get(key) or []
            if len(matches) != 1:
                if len(matches) > 1 and any(int(row.evolution_stage or 0) < floor['stage'] for _, row in matches):
                    ambiguous += 1
                continue
            source, row = matches[0]
            current_stage = max(0, int(row.evolution_stage or 0))
            if current_stage >= floor['stage']:
                continue
            candidates.append({
                'source': source,
                'item_id': int(row.id),
                'adventurer_id': int(row.adventurer_id),
                'item_name': str(row.name),
                'current_stage': current_stage,
                'target_stage': int(floor['stage']),
                'source_log_id': (
                    int(floor['log_id']) if floor.get('log_id') is not None else None
                ),
                'source_transfer_type': floor.get('transfer_type'),
                'source_transfer_id': floor.get('transfer_id'),
                '_row': row,
            })
        candidates.sort(key=lambda item: (item['adventurer_id'], item['item_name'], item['item_id']))
        return candidates, ambiguous

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """Return a dry-run of unambiguous stage restorations."""
        from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
            S1CompanionEvolutionIntegrityScanService,
        )

        artifacts = S1CompanionEvolutionIntegrityScanService.ready_artifacts(
            require_current=True,
        )
        if artifacts is not None:
            return cls.preview_from_artifacts(
                list(artifacts.get('stage_floors') or []),
                dict(artifacts.get('continuity_audit') or {}),
            )
        try:
            candidates, ambiguous = cls._scan()
            expedition_continuity_audit = (
                cls._scan_expedition_continuity_audit()
            )
            adventurer_ids = {item['adventurer_id'] for item in candidates}
            name_map = dict(
                AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
                .filter(AdventurerS1.id.in_(adventurer_ids)).all()
            ) if adventurer_ids else {}
            public_candidates = []
            for item in candidates:
                public_candidates.append({
                    key: value for key, value in item.items() if key != '_row'
                } | {'adventurer_name': name_map.get(item['adventurer_id'], '')})
            backup = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 companion evolution stage repair preview failed')
            return None, f'预览失败：{exc}'
        return {
            'total': len(public_candidates),
            'ambiguous': ambiguous,
            'candidates': public_candidates,
            'has_backup': bool(backup and backup.value),
            'expedition_continuity_audit': expedition_continuity_audit,
        }, None

    @classmethod
    def preview_from_artifacts(
        cls,
        stage_floor_rows: list[dict],
        expedition_continuity_audit: dict,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Build the live dry-run from persisted historical scan artifacts."""
        try:
            candidates, ambiguous = cls._scan(
                cls._stage_floor_map(stage_floor_rows),
            )
            adventurer_ids = {item['adventurer_id'] for item in candidates}
            name_map = dict(
                AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
                .filter(AdventurerS1.id.in_(adventurer_ids)).all()
            ) if adventurer_ids else {}
            public_candidates = [
                {
                    key: value for key, value in item.items() if key != '_row'
                } | {'adventurer_name': name_map.get(item['adventurer_id'], '')}
                for item in candidates
            ]
            backup = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
            has_backup = bool(backup and backup.value)
            db_manager.rollback()
            return {
                'total': len(public_candidates),
                'ambiguous': ambiguous,
                'candidates': public_candidates,
                'has_backup': has_backup,
                'expedition_continuity_audit': expedition_continuity_audit,
            }, None
        except Exception as exc:  # noqa: BLE001
            db_manager.rollback()
            current_app.logger.exception(
                'S1 cached companion evolution stage preview failed',
            )
            return None, f'预览失败：{exc}'

    @staticmethod
    def _successful_evolution_logs() -> list[dict]:
        return list(S1CompanionEvolutionStageRepairService._iter_successful_evolution_logs())

    @staticmethod
    def _legacy_actual_charges_by_usage(logs: list[dict]) -> dict[int, float]:
        """Match legacy success logs to their nearest prior authoritative gold debit."""
        unresolved = [
            log for log in logs
            if isinstance(log.get('result'), dict)
            and log['result'].get('_charged_gold_cost') is None
            and log.get('created_at') is not None
        ]
        if not unresolved:
            return {}
        adventurer_ids = {int(log['adventurer_id']) for log in unresolved}
        building_ids = {str(int(log['building_instance_id'])) for log in unresolved}
        rows = (
            db_manager.query(
                AdventurerS1CurrencyLog.id,
                AdventurerS1CurrencyLog.adventurer_id,
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.change_amount,
                AdventurerS1CurrencyLog.created_at,
            )
            .filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == 'building_usage_cost',
                AdventurerS1CurrencyLog.related_type == 'building_instance',
                AdventurerS1CurrencyLog.related_id.in_(building_ids),
                AdventurerS1CurrencyLog.change_amount < 0,
            )
            .order_by(
                AdventurerS1CurrencyLog.adventurer_id.asc(),
                AdventurerS1CurrencyLog.created_at.asc(),
                AdventurerS1CurrencyLog.id.asc(),
            )
            .all()
        )
        by_pair: dict[tuple[int, str], list[object]] = {}
        for row in rows:
            by_pair.setdefault(
                (int(row.adventurer_id), str(row.related_id)),
                [],
            ).append(row)

        matched: dict[int, float] = {}
        used_currency_log_ids: set[int] = set()
        for log in sorted(unresolved, key=lambda item: (item['created_at'], item['id'])):
            pair = (int(log['adventurer_id']), str(int(log['building_instance_id'])))
            eligible = [
                row for row in by_pair.get(pair, [])
                if int(row.id) not in used_currency_log_ids
                and row.created_at <= log['created_at']
            ]
            if not eligible:
                continue
            row = eligible[-1]
            used_currency_log_ids.add(int(row.id))
            matched[int(log['id'])] = round(abs(float(row.change_amount or 0)), 2)
        return matched

    @staticmethod
    def _fee_usage_log_id(row: AdventurerS1BugReclaimItem) -> Optional[int]:
        result = row.result_json if isinstance(row.result_json, dict) else {}
        usage_log_id = result.get('usage_log_id')
        if usage_log_id is None:
            issue_key = str(row.issue_key or '')
            if issue_key.startswith(_FEE_RECLAIM_ISSUE_PREFIX):
                usage_log_id = issue_key[len(_FEE_RECLAIM_ISSUE_PREFIX):]
        try:
            return int(usage_log_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _player_companion_transfer_events(adventurer_ids: set[int]) -> list[dict]:
        """Load only completed companion transfers touching the player's adventurers."""
        from sqlalchemy import or_

        if not adventurer_ids:
            return []

        events: list[dict] = []
        listing_rows = (
            db_manager.query(
                AdventurerS1MarketListing.id,
                AdventurerS1MarketListing.seller_adventurer_id,
                AdventurerS1MarketListing.buyer_adventurer_id,
                AdventurerS1MarketListing.item_name,
                AdventurerS1MarketListing.evolution_stage,
                AdventurerS1MarketListing.completed_at,
            )
            .filter(
                AdventurerS1MarketListing.status == 'completed',
                AdventurerS1MarketListing.item_type == 'companion',
                AdventurerS1MarketListing.buyer_adventurer_id.isnot(None),
                or_(
                    AdventurerS1MarketListing.seller_adventurer_id.in_(adventurer_ids),
                    AdventurerS1MarketListing.buyer_adventurer_id.in_(adventurer_ids),
                ),
            )
            .all()
        )
        events.extend({
            'event_type': 'market_listing_transfer',
            'event_id': int(row.id),
            'from_adventurer_id': int(row.seller_adventurer_id),
            'to_adventurer_id': int(row.buyer_adventurer_id),
            'item_name': str(row.item_name),
            'stage': (
                int(row.evolution_stage)
                if row.evolution_stage is not None
                and int(row.evolution_stage) > 0
                else None
            ),
            'created_at': row.completed_at,
        } for row in listing_rows)

        barter_rows = (
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
                AdventurerS1MarketOffer.status == 'accepted',
                AdventurerS1MarketOffer.offered_item_type == 'companion',
                or_(
                    AdventurerS1MarketOffer.buyer_adventurer_id.in_(adventurer_ids),
                    AdventurerS1MarketListing.seller_adventurer_id.in_(adventurer_ids),
                ),
            )
            .all()
        )
        events.extend({
            'event_type': 'market_barter_offer_transfer',
            'event_id': int(row.id),
            'from_adventurer_id': int(row.buyer_adventurer_id),
            'to_adventurer_id': int(row.seller_adventurer_id),
            'item_name': str(row.offered_item_name),
            'stage': (
                int(row.offered_evolution_stage)
                if row.offered_evolution_stage is not None
                and int(row.offered_evolution_stage) > 0
                else None
            ),
            'created_at': row.updated_at,
        } for row in barter_rows)

        buy_offer_rows = (
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
                AdventurerS1MarketBuyOffer.status == 'accepted',
                AdventurerS1MarketBuyOffer.item_type == 'companion',
                or_(
                    AdventurerS1MarketBuyOffer.seller_adventurer_id.in_(adventurer_ids),
                    AdventurerS1MarketBuyOrder.buyer_adventurer_id.in_(adventurer_ids),
                ),
            )
            .all()
        )
        events.extend({
            'event_type': 'market_buy_offer_transfer',
            'event_id': int(row.id),
            'from_adventurer_id': int(row.seller_adventurer_id),
            'to_adventurer_id': int(row.buyer_adventurer_id),
            'item_name': str(row.item_name),
            'stage': (
                int(row.evolution_stage)
                if row.evolution_stage is not None
                and int(row.evolution_stage) > 0
                else None
            ),
            'created_at': row.updated_at,
        } for row in buy_offer_rows)
        return events

    @staticmethod
    def _build_player_ledger_lineage(
        evolution_logs: list[dict],
        transfer_events: list[dict],
        audited_stages: dict[int, int],
    ) -> dict[int, dict]:
        """Resolve each evolution from its unique chronological predecessor."""
        timeline: list[tuple[datetime | None, int, int, str, dict]] = []
        for log in evolution_logs:
            timeline.append((
                log.get('created_at'),
                1,
                int(log['id']),
                'evolution',
                log,
            ))
        for transfer in transfer_events:
            timeline.append((
                transfer.get('created_at'),
                0,
                int(transfer['event_id']),
                'transfer',
                transfer,
            ))
        timeline.sort(key=lambda event: (
            event[0] is None,
            event[0] or datetime.max,
            event[1],
            event[2],
        ))

        producers: dict[tuple[int, str], list[dict]] = {}
        lineage_by_usage: dict[int, dict] = {}

        def take_predecessor(key: tuple[int, str]) -> tuple[Optional[dict], str]:
            candidates = producers.get(key) or []
            if not candidates:
                return None, 'missing'
            if len(candidates) == 1:
                predecessor = candidates[0]
                producers.pop(key, None)
                return predecessor, 'unique'
            # One of the same-name instances was consumed, but the persisted
            # evidence cannot identify which. Keep one unknown remainder.
            producers[key] = [{
                'stage': None,
                'source': 'ambiguous_same_name',
            }]
            return None, 'ambiguous'

        for _, _, event_id, event_type, event in timeline:
            if event_type == 'transfer':
                name = str(event.get('item_name') or '').strip()
                if not name:
                    continue
                source_id = int(event['from_adventurer_id'])
                destination_id = int(event['to_adventurer_id'])
                predecessor, predecessor_status = take_predecessor((source_id, name))
                persisted_stage = event.get('stage')
                try:
                    persisted_stage = (
                        int(persisted_stage)
                        if persisted_stage is not None and int(persisted_stage) > 0
                        else None
                    )
                except (TypeError, ValueError):
                    persisted_stage = None
                predecessor_stage = (
                    predecessor.get('stage') if predecessor is not None else None
                )
                if persisted_stage is not None:
                    stage = max(
                        persisted_stage,
                        int(predecessor_stage or 0),
                    )
                    source = 'market_transfer_snapshot'
                elif predecessor_status == 'unique' and predecessor_stage is not None:
                    stage = int(predecessor_stage)
                    source = 'market_transfer_lineage'
                else:
                    stage = None
                    source = 'unknown_market_transfer_stage'
                producers.setdefault((destination_id, name), []).append({
                    'stage': stage,
                    'source': source,
                    'transfer_type': str(event.get('event_type') or ''),
                    'transfer_id': event_id,
                })
                continue

            result = event.get('result')
            if not isinstance(result, dict):
                continue
            old_name = str(result.get('old_name') or '').strip()
            new_name = str(result.get('new_name') or '').strip()
            before = (
                result.get('pre_evolution_snapshot')
                if isinstance(result.get('pre_evolution_snapshot'), dict)
                else {}
            )
            try:
                recorded_from_stage = max(
                    0, int(before.get('evolution_stage') or 0),
                )
            except (TypeError, ValueError):
                recorded_from_stage = 0
            try:
                recorded_to_stage = max(
                    0, int(result.get('evolution_stage') or 0),
                )
            except (TypeError, ValueError):
                recorded_to_stage = recorded_from_stage + 1

            adventurer_id = int(event['adventurer_id'])
            predecessor = None
            predecessor_status = 'missing'
            if old_name:
                predecessor, predecessor_status = take_predecessor(
                    (adventurer_id, old_name),
                )

            audited_stage = audited_stages.get(event_id)
            predecessor_stage = (
                predecessor.get('stage') if predecessor is not None else None
            )
            if predecessor_status == 'ambiguous':
                corrected_from_stage = recorded_from_stage
                stage_is_verified = False
                lineage_evidence = 'ambiguous_same_name'
            elif predecessor_status == 'unique' and predecessor_stage is None:
                corrected_from_stage = recorded_from_stage
                stage_is_verified = False
                lineage_evidence = 'unknown_market_transfer_stage'
            elif predecessor_status == 'unique':
                corrected_from_stage = max(
                    recorded_from_stage,
                    int(predecessor_stage),
                )
                stage_is_verified = True
                predecessor_source = str(predecessor.get('source') or '')
                lineage_evidence = (
                    'market_transfer'
                    if predecessor_source.startswith('market_transfer')
                    else 'previous_evolution'
                )
            elif audited_stage is not None:
                corrected_from_stage = max(recorded_from_stage, int(audited_stage))
                stage_is_verified = True
                lineage_evidence = 'fee_audit'
            else:
                corrected_from_stage = recorded_from_stage
                stage_is_verified = True
                lineage_evidence = 'recorded_snapshot'

            corrected_to_stage = max(
                recorded_to_stage,
                corrected_from_stage + 1,
            )
            lineage_by_usage[event_id] = {
                'corrected_from_stage': corrected_from_stage,
                'corrected_to_stage': corrected_to_stage,
                'stage_is_verified': stage_is_verified,
                'lineage_evidence': lineage_evidence,
                'source_log_id': (
                    predecessor.get('usage_log_id')
                    if predecessor is not None else None
                ),
                'source_transfer_type': (
                    predecessor.get('transfer_type')
                    if predecessor is not None else None
                ),
                'source_transfer_id': (
                    predecessor.get('transfer_id')
                    if predecessor is not None else None
                ),
            }
            if new_name:
                producers.setdefault((adventurer_id, new_name), []).append({
                    'stage': corrected_to_stage if stage_is_verified else None,
                    'source': 'evolution',
                    'usage_log_id': event_id,
                })

        return lineage_by_usage

    @classmethod
    def get_player_ledger(
        cls, user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Return a read-only, owner-scoped companion evolution and fee ledger.

        The ledger is grouped by the persisted adventurer id, so a deceased or
        archived adventurer's adjustments can never be attached to a later live
        adventurer owned by the same account.
        """
        from sqlalchemy.orm import joinedload
        from plugins.adventurer_s1.services.adventurer_s1_building_handler import (
            calculate_pet_evolution_gold_cost,
        )
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import (
            BuildingSettlementService,
        )
        from plugins.adventurer_s1.models import AdventurerS1BuildingInstance
        from utils.time_utils import to_utc_iso

        def money(value: object) -> float:
            try:
                return round(float(value or 0), 2)
            except (TypeError, ValueError):
                return 0.0

        def summarize(events: list[dict]) -> dict:
            complete = all(
                event.get('actual_paid') is not None
                and event.get('correct_due') is not None
                for event in events
            )
            paid_at_evolution = round(sum(
                money(event.get('actual_paid')) for event in events
                if event.get('actual_paid') is not None
            ), 2)
            correct_total_due = round(sum(
                money(event.get('correct_due')) for event in events
                if event.get('correct_due') is not None
            ), 2)
            reclaimed_afterward = round(sum(
                money(event.get('historical_reclaim_deducted')) for event in events
            ), 2)
            reconciled_deducted = round(sum(
                money(event.get('reconciliation_deducted')) for event in events
            ), 2)
            reconciled_refunded = round(sum(
                money(event.get('reconciliation_refunded')) for event in events
            ), 2)
            net_paid = round(
                paid_at_evolution + reclaimed_afterward
                + reconciled_deducted - reconciled_refunded,
                2,
            )
            return {
                'event_count': len(events),
                'paid_at_evolution': paid_at_evolution,
                'correct_total_due': correct_total_due,
                'historical_reclaim_deducted': reclaimed_afterward,
                'reconciliation_deducted': reconciled_deducted,
                'reconciliation_refunded': reconciled_refunded,
                'net_paid': net_paid,
                'remaining_difference': (
                    round(correct_total_due - net_paid, 2) if complete else None
                ),
                'is_complete': complete,
                'unverified_event_count': sum(
                    1 for event in events
                    if event.get('actual_paid') is None
                    or event.get('correct_due') is None
                ),
            }

        try:
            adventurer_rows = (
                db_manager.query(
                    AdventurerS1.id,
                    AdventurerS1.name,
                    AdventurerS1.status,
                )
                .filter(AdventurerS1.owner_id == int(user_id))
                .order_by(AdventurerS1.id.asc())
                .all()
            )
            adventurer_ids = {int(row.id) for row in adventurer_rows}
            if not adventurer_ids:
                empty_summary = summarize([])
                return {
                    'adventurers': [],
                    'summary': empty_summary,
                    'scope': 'owner_bound_adventurers',
                }, None

            usage_rows = (
                db_manager.query(
                    AdventurerS1BuildingUsageLog.id,
                    AdventurerS1BuildingUsageLog.adventurer_id,
                    AdventurerS1BuildingUsageLog.building_instance_id,
                    AdventurerS1BuildingUsageLog.result,
                    AdventurerS1BuildingUsageLog.created_at,
                )
                .filter(
                    AdventurerS1BuildingUsageLog.adventurer_id.in_(adventurer_ids),
                    AdventurerS1BuildingUsageLog.result['type'].as_string() == 'pet_evolution',
                    AdventurerS1BuildingUsageLog.result['success'].as_boolean().is_(True),
                )
                .order_by(
                    AdventurerS1BuildingUsageLog.adventurer_id.asc(),
                    AdventurerS1BuildingUsageLog.created_at.asc(),
                    AdventurerS1BuildingUsageLog.id.asc(),
                )
                .all()
            )
            usage_logs = [{
                'id': int(row.id),
                'adventurer_id': int(row.adventurer_id),
                'building_instance_id': int(row.building_instance_id),
                'result': row.result,
                'created_at': row.created_at,
            } for row in usage_rows]
            usage_log_ids = {int(log['id']) for log in usage_logs}

            reclaim_rows = []
            if usage_log_ids:
                reclaim_rows = (
                    db_manager.query(AdventurerS1BugReclaimItem)
                    .filter(
                        AdventurerS1BugReclaimItem.adventurer_id.in_(adventurer_ids),
                        AdventurerS1BugReclaimItem.status == 'executed',
                        AdventurerS1BugReclaimItem.reverted_at.is_(None),
                        AdventurerS1BugReclaimItem.issue_type.in_((
                            'companion_evolution_fee_evasion',
                            'companion_evolution_fee_reconciliation',
                        )),
                    )
                    .order_by(AdventurerS1BugReclaimItem.id.asc())
                    .all()
                )
            reclaim_by_usage: dict[int, list[AdventurerS1BugReclaimItem]] = {}
            for row in reclaim_rows:
                usage_log_id = cls._fee_usage_log_id(row)
                if usage_log_id in usage_log_ids:
                    reclaim_by_usage.setdefault(usage_log_id, []).append(row)
            audited_stages: dict[int, int] = {}
            for usage_log_id, rows in reclaim_by_usage.items():
                stage_values = []
                for row in rows:
                    if row.issue_type != 'companion_evolution_fee_evasion':
                        continue
                    after = row.after_json if isinstance(row.after_json, dict) else {}
                    try:
                        stage_values.append(max(0, int(after.get('expected_stage'))))
                    except (TypeError, ValueError):
                        pass
                if stage_values:
                    audited_stages[usage_log_id] = max(stage_values)

            lineage_by_usage = cls._build_player_ledger_lineage(
                usage_logs,
                cls._player_companion_transfer_events(adventurer_ids),
                audited_stages,
            )

            building_ids = {
                int(log['building_instance_id']) for log in usage_logs
            }
            building_rows = (
                db_manager.query(AdventurerS1BuildingInstance)
                .options(joinedload(AdventurerS1BuildingInstance.template))
                .filter(AdventurerS1BuildingInstance.id.in_(building_ids))
                .all()
                if building_ids else []
            )
            building_map = {int(row.id): row for row in building_rows}
            legacy_actual_charges = cls._legacy_actual_charges_by_usage(usage_logs)

            ledgers = {
                int(row.id): {
                    'adventurer_name': str(row.name),
                    'adventurer_status': str(row.status),
                    'current_companions': [],
                    'events': [],
                }
                for row in adventurer_rows
            }

            for log in usage_logs:
                result = log['result'] if isinstance(log['result'], dict) else {}
                before = (
                    result.get('pre_evolution_snapshot')
                    if isinstance(result.get('pre_evolution_snapshot'), dict)
                    else {}
                )
                try:
                    recorded_from_stage = max(
                        0, int(before.get('evolution_stage') or 0),
                    )
                except (TypeError, ValueError):
                    recorded_from_stage = 0
                try:
                    recorded_to_stage = max(
                        0, int(result.get('evolution_stage') or 0),
                    )
                except (TypeError, ValueError):
                    recorded_to_stage = recorded_from_stage + 1

                adjustments = reclaim_by_usage.get(int(log['id']), [])
                old_reclaims = [
                    row for row in adjustments
                    if row.issue_type == 'companion_evolution_fee_evasion'
                ]
                reconciliations = [
                    row for row in adjustments
                    if row.issue_type == 'companion_evolution_fee_reconciliation'
                ]
                audited_costs_by_stage: dict[int, list[float]] = {}
                for row in old_reclaims:
                    after = row.after_json if isinstance(row.after_json, dict) else {}
                    try:
                        audit_stage = max(0, int(after.get('expected_stage')))
                        audit_cost = round(float(after.get('expected_cost')), 2)
                    except (TypeError, ValueError):
                        continue
                    audited_costs_by_stage.setdefault(audit_stage, []).append(audit_cost)

                lineage = lineage_by_usage.get(int(log['id']), {})
                corrected_from_stage = int(
                    lineage.get('corrected_from_stage', recorded_from_stage),
                )
                corrected_to_stage = int(lineage.get(
                    'corrected_to_stage',
                    max(recorded_to_stage, corrected_from_stage + 1),
                ))
                stage_is_verified = bool(
                    lineage.get('stage_is_verified', False),
                )
                lineage_evidence = str(
                    lineage.get('lineage_evidence') or 'unavailable',
                )

                charged = result.get('_charged_gold_cost')
                try:
                    actual_paid: Optional[float] = round(float(charged), 2)
                    payment_evidence = 'usage_log'
                except (TypeError, ValueError):
                    if int(log['id']) in legacy_actual_charges:
                        actual_paid = money(legacy_actual_charges[int(log['id'])])
                        payment_evidence = 'currency_log'
                    else:
                        actual_paid = None
                        payment_evidence = 'unavailable'

                matching_audited_costs = audited_costs_by_stage.get(
                    corrected_from_stage,
                    [],
                )
                if stage_is_verified and matching_audited_costs:
                    correct_due: Optional[float] = max(matching_audited_costs)
                elif stage_is_verified:
                    building = building_map.get(int(log['building_instance_id']))
                    if building is None:
                        correct_due = None
                    else:
                        config = BuildingSettlementService.get_reward_config(building)
                        correct_due = calculate_pet_evolution_gold_cost(
                            money(config.get('gold_cost')),
                            corrected_from_stage,
                        )
                else:
                    correct_due = None

                historical_reclaim = round(sum(
                    money(row.gold_deducted) for row in old_reclaims
                ), 2)
                reconciliation_changes = []
                for row in reconciliations:
                    meta = row.result_json if isinstance(row.result_json, dict) else {}
                    reconciliation_changes.append(
                        money(meta.get('net_gold_collected_change')),
                    )
                reconciliation_deducted = round(sum(
                    max(0.0, change) for change in reconciliation_changes
                ), 2)
                reconciliation_refunded = round(sum(
                    max(0.0, -change) for change in reconciliation_changes
                ), 2)

                total_paid = (
                    round(
                        actual_paid + historical_reclaim
                        + reconciliation_deducted - reconciliation_refunded,
                        2,
                    )
                    if actual_paid is not None else None
                )
                difference = (
                    round(correct_due - total_paid, 2)
                    if correct_due is not None and total_paid is not None
                    else None
                )
                if difference is None:
                    account_status = 'unverified'
                elif abs(difference) < 0.01:
                    account_status = 'balanced'
                elif difference > 0:
                    account_status = 'underpaid'
                else:
                    account_status = 'overpaid'

                building = building_map.get(int(log['building_instance_id']))
                building_name = '嬗变祭坛'
                if building is not None:
                    building_name = (
                        str(building.custom_name).strip()
                        if building.custom_name else
                        str(getattr(building.template, 'name', '') or '嬗变祭坛')
                    )
                ledgers[int(log['adventurer_id'])]['events'].append({
                    '_companion_id': (
                        int(result.get('companion_id'))
                        if str(result.get('companion_id') or '').isdigit()
                        else None
                    ),
                    'occurred_at': (
                        to_utc_iso(log['created_at']) if log.get('created_at') else None
                    ),
                    'building_name': building_name,
                    'old_name': str(result.get('old_name') or '伙伴'),
                    'new_name': str(result.get('new_name') or '伙伴'),
                    'recorded_from_stage': recorded_from_stage,
                    'recorded_to_stage': recorded_to_stage,
                    'corrected_from_stage': corrected_from_stage,
                    'corrected_to_stage': corrected_to_stage,
                    'stage_is_verified': stage_is_verified,
                    'lineage_evidence': lineage_evidence,
                    'stage_was_corrected': (
                        corrected_from_stage != recorded_from_stage
                        or corrected_to_stage != recorded_to_stage
                    ),
                    'actual_paid': actual_paid,
                    'payment_evidence': payment_evidence,
                    'correct_due': correct_due,
                    'historical_reclaim_deducted': historical_reclaim,
                    'reconciliation_deducted': reconciliation_deducted,
                    'reconciliation_refunded': reconciliation_refunded,
                    'total_paid_after_adjustment': total_paid,
                    'remaining_difference': difference,
                    'account_status': account_status,
                })

            for source, model in _CONTAINER_MODELS:
                rows = (
                    db_manager.query(
                        model.id,
                        model.adventurer_id,
                        model.name,
                        model.evolution_stage,
                    )
                    .filter(
                        model.adventurer_id.in_(adventurer_ids),
                        model.type == 'companion',
                    )
                    .order_by(model.adventurer_id.asc(), model.id.asc())
                    .all()
                )
                for row in rows:
                    events = ledgers[int(row.adventurer_id)]['events']
                    matching_events = [
                        event for event in events
                        if source == 'equipment'
                        and event.get('_companion_id') == int(row.id)
                    ]
                    if len(matching_events) == 1:
                        lineage_evidence = 'own_evolution'
                    elif len(matching_events) > 1:
                        lineage_evidence = 'ambiguous_own_history'
                    else:
                        lineage_evidence = 'recorded_stage_only'
                    ledgers[int(row.adventurer_id)]['current_companions'].append({
                        'source': source,
                        'name': str(row.name),
                        'current_stage': max(0, int(row.evolution_stage or 0)),
                        'lineage_evidence': lineage_evidence,
                    })

            adventurer_payloads = []
            all_events = []
            for row in adventurer_rows:
                ledger = ledgers[int(row.id)]
                for event in ledger['events']:
                    event.pop('_companion_id', None)
                ledger['summary'] = summarize(ledger['events'])
                all_events.extend(ledger['events'])
                adventurer_payloads.append(ledger)

            return {
                'adventurers': adventurer_payloads,
                'summary': summarize(all_events),
                'scope': 'owner_bound_adventurers',
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 player companion evolution ledger failed')
            return None, player_operation_failure(exc, '读取伙伴嬗变账本失败')

    @classmethod
    def _scan_fee_assessments(
        cls,
        include_usage_log_ids: Optional[set[int]] = None,
        *,
        evolution_logs: Optional[list[dict]] = None,
        transfer_events: Optional[list[dict]] = None,
    ) -> tuple[list[dict], int]:
        """Calculate the correct unpaid difference for confirmed lineages.

        Ordinarily only stage-regression events are returned. Historical reclaim
        usage logs may be explicitly included so a prior false positive can be
        reassessed as zero and refunded by the bidirectional reconciliation.
        """
        from plugins.adventurer_s1.services.s1_building_ai_service import (
            _pet_evolution_gold_cost,
        )
        cost_cache: dict[tuple[int, int], float] = {}

        def cost_for(building_instance_id: int, stage: int) -> float:
            key = (int(building_instance_id), int(stage))
            if key not in cost_cache:
                cost_cache[key] = round(float(_pet_evolution_gold_cost(*key)), 2)
            return cost_cache[key]

        candidates: list[dict] = []
        ambiguous = 0
        evolution_logs = (
            list(evolution_logs)
            if evolution_logs is not None
            else cls._successful_evolution_logs()
        )
        legacy_actual_charges = cls._legacy_actual_charges_by_usage(evolution_logs)
        lineage_by_usage = cls._build_player_ledger_lineage(
            evolution_logs,
            (
                list(transfer_events)
                if transfer_events is not None
                else cls._companion_transfer_events()
            ),
            {},
        )
        for log in evolution_logs:
            result = log['result']
            if not isinstance(result, dict):
                continue
            old_name = str(result.get('old_name') or '').strip()
            new_name = str(result.get('new_name') or '').strip()
            before = result.get('pre_evolution_snapshot')
            if not old_name or not new_name or not isinstance(before, dict):
                continue
            try:
                actual_stage = max(0, int(before.get('evolution_stage') or 0))
            except (TypeError, ValueError):
                continue

            lineage = lineage_by_usage.get(int(log['id'])) or {}
            if not lineage.get('stage_is_verified'):
                ambiguous += 1
                continue
            inherited_stage = max(
                actual_stage,
                int(lineage.get('corrected_from_stage') or 0),
            )
            include_balanced = (
                include_usage_log_ids is not None
                and int(log['id']) in include_usage_log_ids
            )
            if inherited_stage <= actual_stage and not include_balanced:
                continue

            charged_cost = result.get('_charged_gold_cost')
            try:
                charged_cost = round(float(charged_cost), 2)
            except (TypeError, ValueError):
                charged_cost = None
            # The charged amount is evidence of what was actually paid, not
            # evidence of the altar's base price. Always calculate the correct
            # price from the authoritative building configuration, then subtract
            # the actual payment.
            expected_cost = cost_for(log['building_instance_id'], inherited_stage)
            if charged_cost is not None and charged_cost >= 0:
                actual_cost = charged_cost
                payment_evidence = 'usage_log'
            elif log['id'] in legacy_actual_charges:
                actual_cost = legacy_actual_charges[log['id']]
                payment_evidence = 'currency_log'
            else:
                actual_cost = cost_for(log['building_instance_id'], actual_stage)
                payment_evidence = 'legacy_stage_estimate'
            escaped = round(max(0.0, expected_cost - actual_cost), 2)
            source_log_id = lineage.get('source_log_id')
            source_transfer_type = lineage.get('source_transfer_type')
            source_transfer_id = lineage.get('source_transfer_id')
            source_route = (
                {
                    'type': 'pet_evolution_success',
                    'log_id': int(source_log_id),
                }
                if source_log_id is not None
                else {
                    'type': str(source_transfer_type or 'market_transfer'),
                    'transfer_id': int(source_transfer_id),
                }
                if source_transfer_id is not None
                else {
                    'type': 'recorded_pre_evolution_snapshot',
                    'log_id': int(log['id']),
                }
            )
            candidates.append({
                'issue_key': f'{_FEE_RECLAIM_ISSUE_PREFIX}{log["id"]}',
                'issue_type': 'companion_evolution_fee_evasion',
                'usage_log_id': log['id'],
                'adventurer_id': log['adventurer_id'],
                'item_name': old_name,
                'item_type': 'companion',
                'item_rarity': str(before.get('rarity') or 'common'),
                'illegal_quantity': 0,
                'reclaim_item_quantity': 0,
                'gold_to_deduct': escaped,
                'before': {
                    'actual_stage': actual_stage,
                    'actual_cost': actual_cost,
                    'payment_evidence': payment_evidence,
                    'source_log_id': source_log_id,
                    'source_transfer_type': source_transfer_type,
                    'source_transfer_id': source_transfer_id,
                },
                'after': {
                    'expected_stage': inherited_stage,
                    'expected_cost': expected_cost,
                },
                'route': [
                    source_route,
                    {'type': 'stage_regression', 'log_id': log['id']},
                ],
            })
        return candidates, ambiguous

    @classmethod
    def _scan_fee_reclaims(cls) -> tuple[list[dict], int]:
        assessments, ambiguous = cls._scan_fee_assessments()
        return [
            item for item in assessments if float(item.get('gold_to_deduct') or 0) > 0
        ], ambiguous

    @classmethod
    def preview_fee_reclaims(
        cls, admin_user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        try:
            candidates, ambiguous = cls._scan_fee_reclaims()
            issue_keys = [item['issue_key'] for item in candidates]
            processed = set()
            if issue_keys:
                processed = {
                    row[0] for row in db_manager.query(
                        AdventurerS1BugReclaimItem.issue_key,
                    ).filter(
                        AdventurerS1BugReclaimItem.issue_key.in_(issue_keys),
                        AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
                    ).all()
                }
            unprocessed = [
                item for item in candidates if item['issue_key'] not in processed
            ]
            usage_log_ids = {
                int(item['usage_log_id']) for item in unprocessed
            }
            collected = cls._fee_reconciliation_net_by_usage(usage_log_ids)
            pending = []
            already_balanced = 0
            for item in unprocessed:
                usage_log_id = int(item['usage_log_id'])
                correct_due = round(float(item.get('gold_to_deduct') or 0), 2)
                already_collected = round(
                    float(collected.get(usage_log_id, 0.0)),
                    2,
                )
                outstanding = round(
                    max(0.0, correct_due - already_collected),
                    2,
                )
                if outstanding < 0.01:
                    already_balanced += 1
                    continue
                pending.append({
                    **item,
                    'correct_gold_due': correct_due,
                    'already_collected': already_collected,
                    'gold_to_deduct': outstanding,
                })
            adventurer_ids = {item['adventurer_id'] for item in pending}
            adv_rows = AdventurerS1.query.with_entities(
                AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.name,
            ).filter(AdventurerS1.id.in_(adventurer_ids)).all() if adventurer_ids else []
            adv_map = {
                int(row.id): {
                    'user_id': int(row.owner_id) if row.owner_id is not None else None,
                    'adventurer_name': str(row.name),
                }
                for row in adv_rows
            }
            for item in pending:
                item.update(adv_map.get(item['adventurer_id']) or {})
            payload = {
                'items': pending,
                'summary': {
                    'candidate_count': len(pending),
                    'adventurer_count': len(adventurer_ids),
                    'gold_to_deduct': round(sum(
                        float(item['gold_to_deduct']) for item in pending
                    ), 2),
                    'already_processed': len(candidates) - len(unprocessed),
                    'already_balanced_by_reconciliation': already_balanced,
                    'ambiguous_skipped': ambiguous,
                    'calculation_basis': (
                        '每次只按祭坛配置与核准阶级计算正确差价，并减去当时实际已付；'
                        '再扣除旧回收及历次校准已经净收取的金额，只追缴尚未支付的余额。'
                    ),
                },
            }
            token = S1AdminScanSnapshotService.store(
                _FEE_RECLAIM_OPERATION, admin_user_id, payload,
                allow_signed_fallback=True,
            )
            if not token:
                return None, '无法创建预览快照，请稍后重试'
            return {**payload, 'preview_token': token}, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 companion evolution fee reclaim preview failed')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute_fee_reclaims(
        cls, admin_user_id: int, preview_token: str, note: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService

        preview = S1AdminScanSnapshotService.load(
            _FEE_RECLAIM_OPERATION, admin_user_id, preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        items = sorted(
            preview.get('items') or [],
            key=lambda item: (
                int(item.get('adventurer_id') or 0),
                int(item.get('usage_log_id') or 0),
            ),
        )
        if any(
            item.get('correct_gold_due') is None
            or item.get('already_collected') is None
            for item in items
        ):
            return None, '预览快照版本已过期，请重新预览后再执行'
        batch = None
        results = []
        for attempt in range(3):
            now = _portable_utc_now()
            batch = AdventurerS1BugReclaimBatch(
                admin_user_id=admin_user_id,
                status='executed',
                criteria_json={'mode': _FEE_RECLAIM_OPERATION},
                summary_json=preview.get('summary') or {},
                note=(note or '').strip()[:200] or None,
                executed_at=now,
            )
            results = []
            try:
                with db_manager.transaction():
                    S1BugReclaimService._lock_incremental_scan_transaction()
                    adventurer_ids = sorted({
                        int(item['adventurer_id']) for item in items
                    })
                    if adventurer_ids:
                        (
                            AdventurerS1.query
                            .filter(AdventurerS1.id.in_(adventurer_ids))
                            .order_by(AdventurerS1.id.asc())
                            .with_for_update().all()
                        )
                    db_manager.add(batch)
                    issue_keys = [str(item.get('issue_key') or '') for item in items]
                    existing = {
                        row[0] for row in db_manager.query(
                            AdventurerS1BugReclaimItem.issue_key,
                        ).filter(
                            AdventurerS1BugReclaimItem.issue_key.in_(issue_keys),
                            AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
                        ).all()
                    } if issue_keys else set()
                    usage_log_ids = {
                        int(item['usage_log_id']) for item in items
                    }
                    collected = cls._fee_reconciliation_net_by_usage(
                        usage_log_ids,
                    )
                    for item in items:
                        usage_log_id = int(item['usage_log_id'])
                        correct_due = round(
                            float(item.get('correct_gold_due') or 0),
                            2,
                        )
                        current_collected = round(
                            float(collected.get(usage_log_id, 0.0)),
                            2,
                        )
                        outstanding = round(
                            max(0.0, correct_due - current_collected),
                            2,
                        )
                        preview_outstanding = round(
                            float(item.get('gold_to_deduct') or 0),
                            2,
                        )
                        preview_collected = round(
                            float(item.get('already_collected') or 0),
                            2,
                        )
                        if (
                            abs(current_collected - preview_collected) >= 0.01
                            or abs(outstanding - preview_outstanding) >= 0.01
                        ):
                            results.append({
                                **item,
                                'status': 'skipped',
                                'result': {
                                    'message': '预览后账目已变化，跳过；请重新预览',
                                    'current_collected': current_collected,
                                    'current_outstanding': outstanding,
                                },
                            })
                            continue
                        result = S1BugReclaimService._execute_one(
                            item, batch, admin_user_id, now, existing,
                        )
                        results.append(result)
                        if result.get('status') == 'executed':
                            collected[usage_log_id] = round(
                                current_collected + preview_outstanding,
                                2,
                            )
                break
            except Exception as exc:  # noqa: BLE001
                db_manager.rollback()
                error_text = str(exc)
                is_deadlock = (
                    'deadlock detected' in error_text.lower()
                    or 'deadlockdetected' in error_text.lower()
                )
                if is_deadlock and attempt < 2:
                    current_app.logger.warning(
                        'S1 companion evolution fee reclaim deadlock; retrying attempt=%s',
                        attempt + 2,
                    )
                    continue
                current_app.logger.exception('S1 companion evolution fee reclaim failed')
                if is_deadlock:
                    return None, '回收遇到并发锁冲突，已完整回滚，请稍后重新点击执行'
                return None, f'回收失败：{exc}'
        S1AdminScanSnapshotService.delete(
            _FEE_RECLAIM_OPERATION, admin_user_id, preview_token,
        )
        from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
            S1CompanionEvolutionIntegrityScanService,
        )
        S1CompanionEvolutionIntegrityScanService.mark_derived_dirty()
        return {
            'batch': batch.to_dict(),
            'items': results,
            'summary': S1BugReclaimService._summary([
                item for item in results if item.get('status') == 'executed'
            ]),
        }, None

    @staticmethod
    def _fee_reconciliation_net_by_usage(
        usage_log_ids: Optional[set[int]],
    ) -> dict[int, float]:
        """Return net gold already collected by the old reclaim and later corrections."""
        if usage_log_ids is not None and not usage_log_ids:
            return {}
        rows = (
            db_manager.query(AdventurerS1BugReclaimItem)
            .filter(
                AdventurerS1BugReclaimItem.status == 'executed',
                AdventurerS1BugReclaimItem.reverted_at.is_(None),
                (
                    AdventurerS1BugReclaimItem.issue_key.like(
                        f'{_FEE_RECLAIM_ISSUE_PREFIX}%'
                    )
                    | AdventurerS1BugReclaimItem.issue_key.like(
                        f'{_FEE_RECONCILE_ISSUE_PREFIX}%'
                    )
                ),
            )
            .order_by(AdventurerS1BugReclaimItem.id.asc())
            .all()
        )
        totals: dict[int, float] = {}
        for row in rows:
            result = row.result_json if isinstance(row.result_json, dict) else {}
            usage_log_id = S1CompanionEvolutionStageRepairService._fee_usage_log_id(row)
            if usage_log_id is None:
                continue
            if usage_log_ids is not None and usage_log_id not in usage_log_ids:
                continue
            if str(row.issue_key).startswith(_FEE_RECONCILE_ISSUE_PREFIX):
                delta = round(float(result.get('net_gold_collected_change') or 0), 2)
            else:
                delta = round(float(row.gold_deducted or 0), 2)
            totals[usage_log_id] = round(totals.get(usage_log_id, 0.0) + delta, 2)
        return totals

    @classmethod
    def preview_fee_reconciliation(
        cls,
        admin_user_id: int,
        *,
        _assessments: Optional[list[dict]] = None,
        _ambiguous: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Preview bidirectional correction of all prior evolution-fee reclaims."""
        try:
            collected = cls._fee_reconciliation_net_by_usage(None)
            historical_usage_log_ids = set(collected)
            if _assessments is None:
                from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
                    S1CompanionEvolutionIntegrityScanService,
                )
                artifacts = S1CompanionEvolutionIntegrityScanService.ready_artifacts(
                    require_current=True,
                )
                if artifacts is not None:
                    assessments = list(artifacts.get('fee_assessments') or [])
                    ambiguous = int(artifacts.get('fee_ambiguous') or 0)
                else:
                    assessments, ambiguous = cls._scan_fee_assessments(
                        include_usage_log_ids=historical_usage_log_ids,
                    )
            else:
                assessments = list(_assessments)
                ambiguous = int(_ambiguous or 0)
            audited_stages = cls._audited_stage_floors_by_usage()
            usage_log_ids = {int(item['usage_log_id']) for item in assessments}
            unresolved_historical_usage_log_ids = (
                historical_usage_log_ids - usage_log_ids
            )
            items = []
            for assessment in assessments:
                usage_log_id = int(assessment['usage_log_id'])
                correct_due = round(float(assessment.get('gold_to_deduct') or 0), 2)
                already_collected = round(float(collected.get(usage_log_id, 0.0)), 2)
                adjustment = round(correct_due - already_collected, 2)
                before = (
                    assessment.get('before')
                    if isinstance(assessment.get('before'), dict)
                    else {}
                )
                after = (
                    assessment.get('after')
                    if isinstance(assessment.get('after'), dict)
                    else {}
                )
                actual_stage = max(0, int(before.get('actual_stage') or 0))
                expected_stage = max(0, int(after.get('expected_stage') or 0))
                stage_audit_required = (
                    expected_stage > actual_stage
                    and audited_stages.get((
                        int(assessment['adventurer_id']),
                        usage_log_id,
                    ), -1) < expected_stage
                )
                if abs(adjustment) < 0.01:
                    if not stage_audit_required:
                        continue
                items.append({
                    **assessment,
                    'correct_gold_due': correct_due,
                    'already_collected': already_collected,
                    'adjustment_to_apply': adjustment,
                    'gold_to_deduct': max(0.0, adjustment),
                    'gold_to_refund': max(0.0, -adjustment),
                    'stage_audit_required': stage_audit_required,
                })

            adventurer_ids = {int(item['adventurer_id']) for item in items}
            adv_rows = AdventurerS1.query.with_entities(
                AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.name,
            ).filter(AdventurerS1.id.in_(adventurer_ids)).all() if adventurer_ids else []
            adv_map = {
                int(row.id): {
                    'user_id': int(row.owner_id) if row.owner_id is not None else None,
                    'adventurer_name': str(row.name),
                }
                for row in adv_rows
            }
            for item in items:
                item.update(adv_map.get(int(item['adventurer_id'])) or {})

            payload = {
                'items': items,
                'summary': {
                    'candidate_count': len(items),
                    'adventurer_count': len(adventurer_ids),
                    'gold_to_deduct': round(sum(
                        float(item['gold_to_deduct']) for item in items
                    ), 2),
                    'gold_to_refund': round(sum(
                        float(item['gold_to_refund']) for item in items
                    ), 2),
                    'ambiguous_skipped': ambiguous,
                    'unresolved_historical_reclaim_count': len(
                        unresolved_historical_usage_log_ids,
                    ),
                    'calculation_basis': (
                        '每次异常嬗变按祭坛配置和正确阶级重算应付金额，再减去该次实际已付金额；'
                        '最后与旧回收及历次校准的净扣款逐条对账。旧批次曾处理、但新血脉算法'
                        '核准为正常的记录，正确差价按 0 计并退回旧扣款；证据歧义的历史记录不自动退款。'
                        '费用已经对平但正确阶级尚未入审计的记录，仅核准阶级，不增减金币。'
                    ),
                },
            }
            token = S1AdminScanSnapshotService.store(
                _FEE_RECONCILE_OPERATION, admin_user_id, payload,
                allow_signed_fallback=True,
            )
            if not token:
                return None, '无法创建预览快照，请稍后重试'
            return {**payload, 'preview_token': token}, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 companion evolution fee reconciliation preview failed')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute_fee_reconciliation(
        cls, admin_user_id: int, preview_token: str, note: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
            S1CurrencyService,
        )
        from plugins.adventurer_s1.services.s1_bug_reclaim_service import (
            S1BugReclaimService,
        )

        preview = S1AdminScanSnapshotService.load(
            _FEE_RECONCILE_OPERATION, admin_user_id, preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        items = sorted(
            preview.get('items') or [],
            key=lambda item: (
                int(item.get('adventurer_id') or 0),
                int(item.get('usage_log_id') or 0),
            ),
        )

        batch = None
        results = []
        for attempt in range(3):
            now = _portable_utc_now()
            batch = AdventurerS1BugReclaimBatch(
                admin_user_id=admin_user_id,
                status='executed',
                criteria_json={'mode': _FEE_RECONCILE_OPERATION},
                summary_json=preview.get('summary') or {},
                note=(note or '').strip()[:200] or None,
                executed_at=now,
            )
            results = []
            try:
                with db_manager.transaction():
                    S1BugReclaimService._lock_incremental_scan_transaction()
                    adventurer_ids = sorted({
                        int(item['adventurer_id']) for item in items
                    })
                    if adventurer_ids:
                        (
                            AdventurerS1.query
                            .filter(AdventurerS1.id.in_(adventurer_ids))
                            .order_by(AdventurerS1.id.asc())
                            .with_for_update().all()
                        )
                    db_manager.add(batch)

                    usage_log_ids = {int(item['usage_log_id']) for item in items}
                    collected = cls._fee_reconciliation_net_by_usage(usage_log_ids)
                    for item in items:
                        usage_log_id = int(item['usage_log_id'])
                        correct_due = round(float(item.get('correct_gold_due') or 0), 2)
                        already_collected = round(float(collected.get(usage_log_id, 0.0)), 2)
                        adjustment = round(correct_due - already_collected, 2)
                        stage_audit_required = bool(item.get('stage_audit_required'))
                        if abs(adjustment) < 0.01 and not stage_audit_required:
                            results.append({
                                **item,
                                'status': 'skipped',
                                'result': {'message': '当前账目已平衡，跳过'},
                            })
                            continue

                        adjustment_cents = int(round(adjustment * 100))
                        issue_key = (
                            f'{_FEE_RECONCILE_ISSUE_PREFIX}{usage_log_id}:'
                            f'{int(round(correct_due * 100))}:{int(round(already_collected * 100))}:'
                            f'{adjustment_cents}'
                        )
                        existing = db_manager.query(
                            AdventurerS1BugReclaimItem.id,
                        ).filter(
                            AdventurerS1BugReclaimItem.issue_key == issue_key,
                            AdventurerS1BugReclaimItem.status == 'executed',
                        ).first()
                        if existing:
                            results.append({
                                **item,
                                'status': 'skipped',
                                'result': {'message': '该校准已执行，跳过'},
                            })
                            continue

                        deducted = max(0.0, adjustment)
                        refunded = max(0.0, -adjustment)
                        if deducted > 0:
                            _, err = S1CurrencyService.deduct(
                                int(item['adventurer_id']), 'gold', deducted,
                                source_type='evolution_fee_reconcile',
                                related_id=usage_log_id,
                                related_type='building_usage_log',
                                description=f'伙伴嬗变费校准：补扣日志 #{usage_log_id} 的漏缴差价',
                                auto_commit=False,
                                allow_negative=True,
                            )
                        elif refunded > 0:
                            _, err = S1CurrencyService.add(
                                int(item['adventurer_id']), 'gold', refunded,
                                source_type='evolution_fee_reconcile',
                                related_id=usage_log_id,
                                related_type='building_usage_log',
                                description=f'伙伴嬗变费校准：退回日志 #{usage_log_id} 的多扣金额',
                                auto_commit=False,
                            )
                        else:
                            err = None
                        if err:
                            raise RuntimeError(err)

                        before = (
                            item.get('before')
                            if isinstance(item.get('before'), dict)
                            else {}
                        )
                        after = (
                            item.get('after')
                            if isinstance(item.get('after'), dict)
                            else {}
                        )
                        actual_stage = max(0, int(before.get('actual_stage') or 0))
                        expected_stage = max(0, int(after.get('expected_stage') or 0))
                        result_json = {
                            'usage_log_id': usage_log_id,
                            'correct_gold_due': correct_due,
                            'previously_collected': already_collected,
                            'net_gold_collected_change': adjustment,
                            'gold_deducted': deducted,
                            'gold_refunded': refunded,
                            'balance_after_reconciliation': round(
                                already_collected + adjustment, 2,
                            ),
                            'actual_stage': actual_stage,
                            'expected_stage': expected_stage,
                            'stage_audit_only': (
                                stage_audit_required and abs(adjustment) < 0.01
                            ),
                        }
                        if deducted > 0:
                            result_json['revert_snapshot'] = {
                                'version': 1,
                                'mode': 'default',
                                'consumed_items': [],
                                'gold_deducted': deducted,
                            }
                        db_manager.add(AdventurerS1BugReclaimItem(
                            batch=batch,
                            status='executed',
                            issue_key=issue_key,
                            issue_type='companion_evolution_fee_reconciliation',
                            adventurer_id=int(item['adventurer_id']),
                            user_id=item.get('user_id'),
                            item_name=str(item.get('item_name') or '伙伴'),
                            item_type='companion',
                            item_rarity=str(item.get('item_rarity') or 'common'),
                            illegal_quantity=0,
                            reclaimed_item_quantity=0,
                            gold_deducted=deducted,
                            before_json={
                                'correct_gold_due': correct_due,
                                'previously_collected': already_collected,
                                'actual_stage': actual_stage,
                                'actual_cost': before.get('actual_cost'),
                            },
                            after_json={
                                'net_gold_collected': round(
                                    already_collected + adjustment, 2,
                                ),
                                'expected_stage': expected_stage,
                                'expected_cost': after.get('expected_cost'),
                            },
                            route_json=item.get('route') or [],
                            result_json=result_json,
                            executed_by=admin_user_id,
                            executed_at=now,
                        ))
                        collected[usage_log_id] = round(
                            already_collected + adjustment, 2,
                        )
                        results.append({
                            **item,
                            'status': 'executed',
                            'result': result_json,
                        })
                break
            except Exception as exc:  # noqa: BLE001
                db_manager.rollback()
                error_text = str(exc)
                is_deadlock = (
                    'deadlock detected' in error_text.lower()
                    or 'deadlockdetected' in error_text.lower()
                )
                if is_deadlock and attempt < 2:
                    current_app.logger.warning(
                        'S1 companion evolution fee reconciliation deadlock; retrying attempt=%s',
                        attempt + 2,
                    )
                    continue
                current_app.logger.exception('S1 companion evolution fee reconciliation failed')
                if is_deadlock:
                    return None, '校准遇到并发锁冲突，已完整回滚，请稍后重新执行'
                return None, f'校准失败：{exc}'

        S1AdminScanSnapshotService.delete(
            _FEE_RECONCILE_OPERATION, admin_user_id, preview_token,
        )
        from plugins.adventurer_s1.services.s1_companion_evolution_integrity_scan_service import (
            S1CompanionEvolutionIntegrityScanService,
        )
        S1CompanionEvolutionIntegrityScanService.mark_derived_dirty()
        executed = [
            item for item in results if item.get('status') == 'executed'
        ]
        return {
            'batch': batch.to_dict(),
            'items': results,
            'summary': {
                'count': len(executed),
                'gold_deducted': round(sum(
                    float(item['result'].get('gold_deducted') or 0) for item in executed
                ), 2),
                'gold_refunded': round(sum(
                    float(item['result'].get('gold_refunded') or 0) for item in executed
                ), 2),
            },
        }, None

    @classmethod
    def repair(cls, updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """Restore only lower stages, with a reversible backup and one transaction."""
        try:
            candidates, ambiguous = cls._scan()
            if not candidates:
                return {'repaired': 0, 'ambiguous': ambiguous}, None

            with db_manager.transaction():
                candidate_by_identity = {
                    (item['source'], item['item_id']): item for item in candidates
                }
                current_rows = cls._load_current_companions(
                    {item['adventurer_id'] for item in candidates},
                    lock=True,
                )
                current_by_key: dict[tuple[int, str], list[tuple[str, object]]] = {}
                for source, row in current_rows:
                    current_by_key.setdefault(
                        (int(row.adventurer_id), str(row.name)), [],
                    ).append((source, row))

                backup_entries = []
                for source, row in current_rows:
                    item = candidate_by_identity.get((source, int(row.id)))
                    if item is None:
                        continue
                    matches = current_by_key.get((int(row.adventurer_id), str(row.name))) or []
                    if len(matches) != 1:
                        continue
                    if (
                        int(row.adventurer_id) != item['adventurer_id']
                        or str(row.name) != item['item_name']
                        or str(row.type or '') != 'companion'
                    ):
                        continue
                    old_stage = max(0, int(row.evolution_stage or 0))
                    target_stage = max(old_stage, int(item['target_stage']))
                    if old_stage != int(item['current_stage']) or target_stage <= old_stage:
                        continue
                    row.evolution_stage = target_stage
                    backup_entries.append({
                        'source': source,
                        'item_id': item['item_id'],
                        'adventurer_id': item['adventurer_id'],
                        'item_name': item['item_name'],
                        'old_stage': old_stage,
                        'repaired_stage': target_stage,
                        'source_log_id': item['source_log_id'],
                    })

                if not backup_entries:
                    return {'repaired': 0, 'ambiguous': ambiguous}, None

                payload = json.dumps({
                    'created_at': _portable_utc_now().isoformat() + 'Z',
                    'entries': backup_entries,
                }, ensure_ascii=False)
                backup = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if backup is None:
                    ok, err = db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='伙伴嬗变阶位存量修复备份，可一键还原',
                        updated_by_user_id=updated_by_user_id,
                    ))
                    if not ok:
                        raise RuntimeError(err)
                else:
                    backup.value = payload
                    backup.updated_by_user_id = updated_by_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 companion evolution stage repair failed')
            return None, f'修复失败：{exc}'

        cls._invalidate_adventurers({entry['adventurer_id'] for entry in backup_entries})
        return {'repaired': len(backup_entries), 'ambiguous': ambiguous, 'entries': backup_entries}, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """Restore the most recent repair when the item identity and repaired stage still match."""
        backup = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not backup or not backup.value:
            return None, '没有可用的伙伴嬗变阶位修复备份'
        try:
            payload = json.loads(backup.value)
            entries = [entry for entry in payload.get('entries', []) if isinstance(entry, dict)]
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None, '备份数据已损坏，无法还原'

        try:
            with db_manager.transaction():
                adventurer_ids = {
                    int(entry['adventurer_id']) for entry in entries if entry.get('adventurer_id') is not None
                }
                current = cls._load_current_companions(adventurer_ids, lock=True)
                row_map = {(source, int(row.id)): row for source, row in current}
                restored_entries = []
                for entry in entries:
                    key = (str(entry.get('source') or ''), int(entry.get('item_id') or 0))
                    row = row_map.get(key)
                    if not row:
                        continue
                    if int(row.adventurer_id) != int(entry.get('adventurer_id') or 0):
                        continue
                    if str(row.name) != str(entry.get('item_name') or ''):
                        continue
                    if int(row.evolution_stage or 0) != int(entry.get('repaired_stage') or 0):
                        continue
                    row.evolution_stage = max(0, int(entry.get('old_stage') or 0))
                    restored_entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 companion evolution stage repair restore failed')
            return None, f'还原失败：{exc}'

        cls._invalidate_adventurers({entry['adventurer_id'] for entry in restored_entries})
        return {
            'restored': len(restored_entries),
            'backup_at': payload.get('created_at'),
        }, None

    @staticmethod
    def _invalidate_adventurers(adventurer_ids: set[int]) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_adventurer_detail_cache

        for adventurer_id in adventurer_ids:
            invalidate_adventurer_detail_cache(int(adventurer_id))
