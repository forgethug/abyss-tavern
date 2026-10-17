"""Short transactions for permanent temporary-location identities."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog, AdventurerS1WorldLocationState
from plugins.adventurer_s1.services.s1_temporary_location_service import (
    TEMPORARY_LOCATION_FIELD, TEMPORARY_LOCATION_NAMESPACE, temporary_location_from_context,
)


def freeze_temporary_location(log_id, target_dungeon, proposal, *, generation_claim_token):
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        _owns_expedition_generation, _reject_lost_expedition_generation_claim,
    )
    from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_DUNGEONS
    from plugins.adventurer_s1.services.s1_expedition_locus_service import (
        EXPEDITION_LOCUS_LOG_FIELD, location_scope_key,
    )

    with db_manager.transaction():
        from db_repository.s1_world_campaign_repository import S1WorldCampaignRepository
        # Share the catalog reservation lock; no AI or network work occurs here.
        S1WorldCampaignRepository.acquire_campaign_write_lock()
        log = (AdventurerS1ExpeditionLog.query.filter_by(id=log_id)
               .with_for_update().populate_existing().first())
        if not log or log.status != 'generating' or (
            generation_claim_token is not None
            and not _owns_expedition_generation(log, generation_claim_token)
        ):
            _reject_lost_expedition_generation_claim(generation_claim_token)
            raise ValueError('temporary_location_generation_not_active')
        full_log = dict(log.full_log or {})
        frozen = temporary_location_from_context(full_log)
        if frozen:
            if frozen['base_dungeon'] != target_dungeon:
                raise ValueError('temporary_location_base_mismatch')
            return frozen
        if not proposal:
            return None
        if log.target_dungeon != target_dungeon:
            raise ValueError('temporary_location_base_mismatch')
        if any(full_log.get(key) for key in (
            'hidden_dungeon_context', 'hidden_boss_context', 'special_dungeon_context', 'commission_context',
        )):
            raise ValueError('temporary_location_conflicts_with_fixed_destination')
        name = proposal['location_name']
        from plugins.adventurer_s1.services.s1_campaign_world_service import campaign_world
        if campaign_world(target_dungeon):
            raise ValueError('activity_world_has_fixed_destination')
        if campaign_world(name):
            raise ValueError('temporary_location_is_activity_dungeon')
        if name == target_dungeon or name in {row['name'] for row in S1_DUNGEONS}:
            raise ValueError('temporary_location_is_configured_dungeon')
        _, error = db_manager.execute_raw(
            'INSERT INTO adventurer_s1_world_location_states '
            '(target_dungeon, canonical_name, current_state, revision, created_at, updated_at) '
            "VALUES (:dungeon, :name, '', 0, :now, :now) "
            'ON CONFLICT (target_dungeon, canonical_name) DO NOTHING',
            {'dungeon': TEMPORARY_LOCATION_NAMESPACE, 'name': name, 'now': _portable_utc_now()},
        )
        if error:
            raise RuntimeError(f'temporary_location_identity_reservation_failed: {error}')
        row = (AdventurerS1WorldLocationState.query.filter_by(
            target_dungeon=TEMPORARY_LOCATION_NAMESPACE, canonical_name=name,
        ).with_for_update().populate_existing().one())
        frozen = {
            'schema_version': 1, 'location_id': row.id, 'location_name': row.canonical_name,
            'base_dungeon': target_dungeon, 'entry_basis': proposal['entry_basis'],
        }
        full_log[TEMPORARY_LOCATION_FIELD] = frozen
        full_log['location_state_snapshot'] = {
            'target_dungeon': target_dungeon,
            'is_explorable': True, 'identity_is_persistent': True,
            TEMPORARY_LOCATION_FIELD: frozen,
            'locations': [{
                'location_id': row.id, 'location_name': row.canonical_name,
                'revision': row.revision, 'current_state': row.current_state,
                'public_risk_notice': row.public_risk_notice or '',
            }],
            'captured_at': _portable_utc_now().isoformat(),
        }
        full_log[EXPEDITION_LOCUS_LOG_FIELD] = {
            'schema_version': 1, 'expedition_log_id': log_id, 'target_dungeon': target_dungeon,
            'kind': 'known_location', 'scope_key': location_scope_key(row.id),
            'location_id': row.id, 'location_name': row.canonical_name,
            'selected_revision': row.revision, 'captured_at': _portable_utc_now().isoformat(),
        }
        log.full_log = full_log
        return frozen


def location_namespace_for_log(log_id: int, target_dungeon: str) -> tuple[str, int | None]:
    """Resolve write namespace from persisted server state, never an AI snapshot."""
    log = AdventurerS1ExpeditionLog.query.filter_by(id=log_id).first()
    destination = temporary_location_from_context(log.full_log if log else None)
    if destination:
        if destination['base_dungeon'] != target_dungeon:
            raise ValueError('temporary_location_base_mismatch')
        return TEMPORARY_LOCATION_NAMESPACE, destination['location_id']
    return target_dungeon, None
