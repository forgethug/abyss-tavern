"""Persistence on the existing dungeon-maintenance transaction and claim."""
from __future__ import annotations

from copy import deepcopy

from flask import current_app
from sqlalchemy import exists
from db_repository import db_manager
from db_repository.s1_world_actor_identity_repository import actor_name_exists, prompt_name_registry
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog as Log,
    AdventurerS1WorldFactCandidate as Candidate,
    AdventurerS1WorldActor as Actor,
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1WorldStoryThreadActor as Link,
    AdventurerS1WorldLocationState as Location,
)
from plugins.adventurer_s1.services.s1_encounter_preparation_contract import (
    digest, normalize_proposal, normalize_review,
)
from plugins.adventurer_s1.services.s1_encounter_preparation_service import (
    eligible, source_projection,
)


def encounter_pool_thread_clause(*, active_only: bool = False):
    """An exact persisted role separates encounter storage from story capacity."""
    linked = (Link.world_story_thread_id == Thread.id,
              Link.narrative_role == 'encounter_pool')
    if active_only:
        linked += (Link.actor_id == Actor.id, Actor.status == 'active',
                   Link.involvement == 'direct', Link.lease_required.is_(True))
    return exists().where(*linked)


def ensure_observed_actor_carrier(actor, *, log, candidate, dungeon):
    """Archive an individual's recall link, without creating a public location."""
    from plugins.adventurer_s1.services.s1_expedition_locus_service import (
        resolve_persistent_story_scope_from_full_log, normalize_expedition_locus_snapshot,
    )
    linked = (Thread.query.join(Link, Link.world_story_thread_id == Thread.id)
              .filter(Link.actor_id == actor.id, Link.involvement == 'direct',
                      Link.lease_required.is_(True), Thread.base_dungeon == dungeon,
                      Thread.is_chain_head.is_(True),
                      Thread.status.in_(('seeded', 'active', 'escalated', 'closing')))
              .order_by(Thread.id).first())
    if linked:
        return linked.id
    status, scope_key = resolve_persistent_story_scope_from_full_log(
        log.full_log, expected_target_dungeon=dungeon, expected_expedition_log_id=log.id)
    valid_locus = normalize_expedition_locus_snapshot(
        log.full_log.get('_expedition_locus_snapshot'),
        expected_target_dungeon=dungeon, expected_expedition_log_id=log.id)
    if status == 'unresolved' and not valid_locus:
        raise ValueError('first_encounter_actor.source_scope_invalid')
    thread = Thread(
        title=f'{actor.canonical_name} · 实体档案 {actor.id}', thread_type='entity',
        status='seeded', base_dungeon=dungeon,
        scope_keys=[dungeon] + ([scope_key] if scope_key else []),
        summary=actor.identity_summary, source_candidate_ids=[candidate.id],
        last_seen_log_id=log.id, is_chain_head=True,
    )
    db_manager.add(thread, flush=False)
    ok, error = db_manager.flush()
    if not ok:
        raise RuntimeError(error or 'first_encounter_actor.carrier_failed')
    db_manager.add(Link(world_story_thread_id=thread.id, actor_id=actor.id,
                        involvement='direct', narrative_role='encounter_pool',
                        lease_required=True), flush=False)
    return thread.id


def _pending_capacity_available(dungeon: str) -> bool:
    limit = int(current_app.config.get('ADVENTURER_S1_ENCOUNTER_PREPARATION_PENDING_LIMIT', 0))
    return limit <= 0 or _pending_count(dungeon) < limit


def _pending_count(dungeon: str) -> int:
    return (Actor.query.join(Link, Link.actor_id == Actor.id)
            .join(Thread, Thread.id == Link.world_story_thread_id)
            .filter(Thread.base_dungeon == dungeon, Thread.status == 'seeded',
                    Thread.injection_count == 0, Actor.status == 'active',
                    Actor.first_seen_log_id.is_(None),
                    Actor.capability_source_log_id.isnot(None)).count())


def _world_context(dungeon: str) -> dict:
    """Bound the current world view independently of archived pool size."""
    limit = max(1, int(current_app.config.get('ADVENTURER_S1_WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT', 20)))
    threads = Thread.query.filter(
        Thread.base_dungeon == dungeon, Thread.is_chain_head.is_(True),
        Thread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
    ).order_by(Thread.updated_at.desc(), Thread.id.desc()).limit(limit).all()
    actors = (Actor.query.join(Link, Link.actor_id == Actor.id)
              .filter(Link.world_story_thread_id.in_([row.id for row in threads]))
              .order_by(Actor.id).all()) if threads else []
    return {
        'threads': [{key: deepcopy(getattr(row, key)) for key in
                     ('id', 'title', 'status', 'summary', 'revision')} for row in threads],
        'actors': [{key: deepcopy(getattr(row, key)) for key in
                    ('id', 'canonical_name', 'aliases', 'status', 'identity_summary',
                     'current_state', 'current_agenda', 'capability_profile', 'capability_revision')}
                   for row in actors],
    }


def project_maintenance_plan(value: dict | None) -> dict:
    return {key: deepcopy((value or {}).get(key) or []) for key in (
        'world_story_threads', 'world_actors', 'location_state_changes', 'faction_profile_updates',
    )}


def validate_context(snapshot: dict) -> str | None:
    """Run after claim checks, BEFORE this batch mutates its own world."""
    frozen = snapshot['location_snapshot']['locations']
    current = (Location.query.filter(Location.id.in_([row['location_id'] for row in frozen]))
               .order_by(Location.id).with_for_update().populate_existing().all()) if frozen else []
    if {row.id: row.revision for row in current} != {
            row['location_id']: row['revision'] for row in frozen}:
        return 'location_changed'
    if digest(_world_context(snapshot['target_dungeon'])) != digest(snapshot['world_context']):
        return 'world_context_changed'
    return None


def build_snapshot(dungeon: str, candidate_ids: list[int], *, maintenance_plan: dict | None = None,
                   skip_reason_out: dict | None = None) -> dict | None:
    from plugins.adventurer_s1.services.adventurer_s1_config_service import (
        resolve_s1_special_event_kind, get_s1_dungeon_level_range,
        get_s1_special_core_level_reference,
    )
    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
    from plugins.adventurer_s1.services.s1_expedition_locus_service import (
        resolve_persistent_story_scope_from_full_log, normalize_expedition_locus_snapshot,
    )
    from plugins.adventurer_s1.services.s1_ai_player_name_service import frozen_player_names
    from plugins.adventurer_s1.services.s1_world_actor_service import actor_prompt_core_budget

    def skip(reason, **details):
        if skip_reason_out is not None:
            skip_reason_out.update(reason_code=reason, **details)
        return None

    rows = (Candidate.query.join(Log, Log.id == Candidate.expedition_log_id)
            .filter(Candidate.id.in_(candidate_ids), Candidate.target_dungeon == dungeon,
                    Log.status == 'completed')
            .with_entities(Candidate.id, Log.id, Log.full_log)
            .order_by(Log.id.desc()).all())
    selected = next(((cid, lid, full) for cid, lid, full in rows
                     if eligible(full) and full.get('settlement_receipt', {}).get('status') == 'passed'), None)
    if selected is None:
        return skip('no_settled_special_source')
    cid, lid, full = selected
    source = source_projection(full)
    if (not source['events'] or not all(isinstance(x, str) and x.strip() for x in source['events'])
            or not source['party']):
        return skip('settled_evidence_incomplete', source_log_id=int(lid))
    scope_status, scope_key = resolve_persistent_story_scope_from_full_log(
        full, expected_target_dungeon=dungeon, expected_expedition_log_id=int(lid))
    location = S1WorldLocationStateService.build_snapshot(
        dungeon, preserve_persisted_explorability=True)
    location = {key: location[key] for key in ('target_dungeon', 'is_explorable', 'locations')}
    pending_count = _pending_count(dungeon)
    allow_prepare = (source['party_ability_status'] == 'complete'
                     and location['is_explorable'] is not False and _pending_capacity_available(dungeon))
    # A valid frontier can establish an individual's identity without having
    # created a persistent public location. Invalid/mismatched loci stay closed.
    valid_locus = normalize_expedition_locus_snapshot(
        full.get('_expedition_locus_snapshot'), expected_target_dungeon=dungeon,
        expected_expedition_log_id=int(lid),
    )
    allow_observed = scope_status != 'unresolved' or bool(valid_locus)
    # Final narrative may introduce a surviving individual without an outline
    # profile (including a quarantined outline). The paired proposal review
    # establishes that identity from indexed final events before admission.
    observed_available = allow_observed and bool(source['events'])
    if not allow_prepare and not observed_available:
        if source['party_ability_status'] != 'complete':
            return skip('source_abilities_unavailable', source_log_id=int(lid))
        return skip('no_available_entity_path', source_log_id=int(lid), pending_count=pending_count,
                    location_closed=location['is_explorable'] is False)
    world_context = _world_context(dungeon)
    registry = prompt_name_registry(priority_actor_ids=[row['id'] for row in world_context['actors']])
    names = list(dict.fromkeys(name for row in registry
                              for name in [row['canonical_name'], *row['aliases']] if isinstance(name, str)))
    kind = resolve_s1_special_event_kind(full)
    reference = get_s1_special_core_level_reference(
        get_s1_dungeon_level_range(dungeon)[0], special_event_kind=kind,
        party_highest_level=max(int(m.get('level') or 1) for m in source['party']),
    )
    snapshot = {
        **source, 'target_dungeon': dungeon, 'source_candidate_id': int(cid),
        'source_log_id': int(lid), 'source_hash': digest(source),
        'scope_keys': [dungeon] + ([scope_key] if scope_key else []),
        'special_event_kind': kind, 'core_level_reference': reference,
        'world_actor_name_registry': names, 'location_snapshot': location,
        'world_context': world_context,
        'maintenance_plan': project_maintenance_plan(maintenance_plan),
        'allow_observed': allow_observed,
        'observed_location_scope': scope_status,
        'allow_prepare': allow_prepare,
        'player_names': list(frozen_player_names(full)),
        'contract_limits': {
            'name_chars': 120,
            'text_chars': int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_FIELD_MAX_CHARS', 320)),
            'capability_chars': int(current_app.config.get('ADVENTURER_S1_ENCOUNTER_PREPARATION_CAPABILITY_CHARS', 1200)),
            'actor_core_chars': actor_prompt_core_budget(),
        },
    }
    # The shared gateway owns the final system + user budget after projection.
    return snapshot


def apply_preparation(result: dict, *, dungeon: str, candidate_ids: list[int],
                      context_verified: bool = False) -> dict:
    """Caller holds the current dungeon claim and candidate rows; never commits.

    Invoked in an auxiliary savepoint, after ordinary maintenance. Input/state
    drift rejects this proposal only. Existing identities are never overwritten.
    """
    receipt = {key: deepcopy(result[key]) for key in (
        'status', 'source_log_id', 'source_hash', 'proposal', 'proposal_hash', 'review', 'reason_code',
    ) if key in result}
    if result.get('status') != 'approved':
        return receipt
    snapshot = result.get('_snapshot')
    if (not isinstance(snapshot, dict) or snapshot.get('target_dungeon') != dungeon
            or snapshot.get('source_candidate_id') not in candidate_ids
            or result.get('source_hash') != snapshot.get('source_hash')
            or result.get('source_log_id') != snapshot.get('source_log_id')
            or result.get('review', {}).get('approved') is not True
            or result.get('proposal_hash') != digest(result.get('proposal'))):
        return {**receipt, 'status': 'stale', 'reason_code': 'binding_invalid'}
    candidate = Candidate.query.filter_by(id=snapshot['source_candidate_id']).first()
    log = Log.query.filter_by(id=snapshot['source_log_id']).first()
    if (candidate is None or candidate.expedition_log_id != snapshot['source_log_id']
            or candidate.target_dungeon != dungeon or log is None or log.status != 'completed'
            or not eligible(log.full_log) or digest(source_projection(log.full_log)) != snapshot['source_hash']):
        return {**receipt, 'status': 'stale', 'reason_code': 'source_changed'}
    entity = normalize_proposal(result['proposal'], snapshot)['entity']
    if entity is None:
        return {**receipt, 'status': 'no_candidate'}
    if digest({'entity': entity}) != result['proposal_hash']:
        return {**receipt, 'status': 'stale', 'reason_code': 'proposal_contract_changed'}
    review = normalize_review(result['review'], snapshot=snapshot, proposal={'entity': entity})
    receipt['review'] = review
    if not context_verified:
        stale_reason = validate_context(snapshot)
        if stale_reason:
            return {**receipt, 'status': 'stale', 'reason_code': stale_reason}
    from plugins.adventurer_s1.models import AdventurerS1WorldOutlineDungeonState
    state = AdventurerS1WorldOutlineDungeonState.query.filter_by(target_dungeon=dungeon).first()
    if entity['origin'] == 'prepared' and (
        (state is not None and state.is_explorable is False)
        or not _pending_capacity_available(dungeon)
    ):
        return {**receipt, 'status': 'deferred', 'reason_code': 'pending_capacity_or_closed'}
    if (actor_name_exists(entity['canonical_name'])
            or Thread.query.filter_by(title=entity['entry_title']).first()):
        return {**receipt, 'status': 'deferred', 'reason_code': 'identity_already_exists'}

    # These are existing models with existing leases, reset and injection consumers.
    # A preparation source is provenance, not a fictitious prior sighting.
    seen_log_id = snapshot['source_log_id'] if entity['origin'] == 'observed' else None
    actor = Actor(
        canonical_name=entity['canonical_name'], actor_type='individual', status='active',
        identity_summary=entity['identity_summary'], stable_principles=entity['stable_principles'],
        long_term_goal=entity['long_term_goal'], current_agenda=entity['current_agenda'],
        current_state=entity['current_state'], capability_profile=deepcopy(entity['capability_profile']),
        capability_revision=1, capability_source_log_id=snapshot['source_log_id'],
        scope_keys=list(snapshot['scope_keys']), source_candidate_ids=[candidate.id],
        first_seen_log_id=seen_log_id, last_seen_log_id=seen_log_id,
    )
    thread = Thread(
        title=entity['entry_title'], thread_type='entity', status='seeded',
        base_dungeon=dungeon, scope_keys=list(snapshot['scope_keys']),
        summary=entity['entry_summary'], source_candidate_ids=[candidate.id],
        last_seen_log_id=seen_log_id, is_chain_head=True,
    )
    db_manager.add(actor, flush=False)
    db_manager.add(thread, flush=False)
    ok, error = db_manager.flush()
    if not ok:
        raise RuntimeError(error or 'encounter_preparation.flush_failed')
    db_manager.add(Link(
        world_story_thread_id=thread.id, actor_id=actor.id,
        involvement='direct', narrative_role='encounter_pool', lease_required=True,
    ), flush=False)
    ok, error = db_manager.flush()
    if not ok:
        raise RuntimeError(error or 'encounter_preparation.link_failed')
    if entity['origin'] == 'observed':
        from plugins.adventurer_s1.services.s1_first_encounter_learning_service import persist
        learning = result.get('_first_encounter_learning')
        if not isinstance(learning, dict):
            raise ValueError('first_encounter_learning.admission_receipt_missing')
        receipt['first_encounter_learning'] = persist(
            learning, actor, dungeon=dungeon, candidate_ids=candidate_ids,
            source_profile_index=review.get('source_profile_index'))
    return {**receipt, 'status': 'persisted', 'actor_id': actor.id, 'world_story_thread_id': thread.id,
            'capability_revision': actor.capability_revision}
