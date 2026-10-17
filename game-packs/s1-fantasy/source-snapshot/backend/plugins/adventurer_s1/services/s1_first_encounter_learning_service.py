"""First-encounter learning is prepared before admission and bound in its transaction.

The AI phase uses an isolated admission-local identity, never a guessed database ID.
Only the creator's returned ID can bind these records to a persisted actor.
"""
from copy import deepcopy

from db_repository import db_manager
from plugins.adventurer_s1.services.s1_encounter_preparation_contract import digest

_SOURCE_PROFILE_INDEX_UNSET = object()


class FirstEncounterLearningError(RuntimeError):
    """Expose failed first learning instead of consuming it as a successful skip."""

    def __init__(self, message, *, phase=None, error_code=None, metadata=None):
        super().__init__(message)
        self.phase = phase
        self.error_code = error_code
        self.failure_stage = f'first_encounter_learning_{phase}' if phase else None
        # Carry only bounded gateway diagnostics; raw requests and responses
        # remain in the learning call's existing token-usage evidence.
        metadata = metadata or {}
        self.upstream_failure = {
            field: str(metadata[key])[:limit]
            for field, key, limit in (
                ('stage', 'last_failed_attempt_stage', 80),
                ('reason_code', 'last_failed_attempt_reason_code', 120),
                ('validation_error_code', 'last_failed_attempt_validation_error_code', 160),
            ) if metadata.get(key)
        }


def source_fingerprint(full):
    return digest({key: full.get(key) for key in (
        'events', 'member_updates', 'audit_member_snapshot', 'objective_outline',
        '_member_id_to_name', 'settlement_receipt', '_expedition_locus_snapshot',
    )})


def learning_context(full, actor, actor_id):
    """Private learning view; never change the original departure/outline snapshot."""
    view = deepcopy(full)
    view['objective_outline'] = {'encounter_capability_profiles': [{
        'scope': 'individual', 'world_actor_id': actor_id, 'capability_revision': 1,
    }]}
    view['objective_world_intel_injection'] = {'world_actors': [{
        **deepcopy(actor), 'id': actor_id, 'capability_revision': 1,
    }]}
    view['generation_world_intel_injection'] = {}
    # Published events use the exact frozen name map. Audit display names may
    # still be M-codenames; build_learning_input already falls back to them only
    # when the frozen map has no name. Never replace an authoritative mapping.
    view.pop('_countermeasure_learning_owner', None)
    view.pop('countermeasure_learning', None)
    return view


def extract(full, actor, *, source_log_id, source_candidate_id):
    """Reuse both learning owners; defer persistence until a real identity exists."""
    from flask import current_app
    from plugins.adventurer_s1.services import s1_countermeasure_learning_ai_service as L
    from plugins.adventurer_s1.services import s1_countermeasure_review_design_service as D
    from plugins.adventurer_s1.services.s1_ai_player_name_service import frozen_player_names
    actor = {key: deepcopy(actor[key]) for key in (
        'canonical_name', 'identity_summary', 'capability_profile',
        'current_state', 'current_agenda', 'power_boundary',
    ) if key in actor}
    receipt = dict(source_log_id=source_log_id, source_candidate_id=source_candidate_id,
                   source_fingerprint=source_fingerprint(full), actor=actor,
                   actor_fingerprint=digest(actor), records=[])
    if not current_app.config.get('ADVENTURER_S1_COUNTERMEASURE_LEARNING_ENABLED', True):
        return {**receipt, 'status': 'disabled'}
    learning_input = L.build_learning_input({}, learning_context(full, actor, 1))
    # Scope 1 is local to this immutable admission proposal. It cannot reference
    # any existing memory or escape to the database without exact rebinding.
    learning_input['actor_identity_scope'] = (
        'admission_local：actors 的 id 是本次新主体提案内编号，'
        'observer_world_actor_id 原样引用该编号；后端建档时绑定真实身份。'
    )
    profile = L.resolve_s1_ai_profile_snapshot(L.FEATURE)
    args = dict(profile=profile, party_id=None, expedition_log_id=source_log_id,
                physical_attempt_guard=None, player_names=frozen_player_names(full))
    db_manager.remove()
    payload = D.build_review_view(learning_input)
    normalized, error, metadata = L._call_phase(
        'review', payload, lambda value: D.normalize_review(value, learning_input), **args)
    if error:
        raise FirstEncounterLearningError(
            f'first_encounter_learning.review: {error}',
            phase='review', error_code=error, metadata=metadata)
    reviewed, rejected = normalized
    receipt.update(review_metadata=metadata, rejected_review=rejected, reviewed=reviewed,
                   review_format=D.REVIEW_FORMAT, source_identity_repairs=[
                       repair for row in reviewed for repair in row.get('source_identity_repairs', [])])
    records = []
    if reviewed:
        payload = D.build_design_view(learning_input, reviewed)
        normalized, error, metadata = L._call_phase(
            'experience', payload, lambda value: D.normalize_design(value, reviewed, learning_input, sparse_updates=True), **args)
        if error:
            raise FirstEncounterLearningError(
                f'first_encounter_learning.experience: {error}',
                phase='experience', error_code=error, metadata=metadata)
        experiences, rejected = normalized
        receipt.update(experience_metadata=metadata, rejected_experiences=rejected, experiences=experiences)
        records, rejected = D.seal_review_sources(reviewed, experiences, learning_input)
        receipt.update(rejected_archive=rejected)
        if any('_capability_growth' in row for row in experiences):
            from .s1_countermeasure_growth_service import review_growth
            receipt['capability_growth'] = review_growth(learning_input, reviewed, experiences, args)
    receipt.update(status='completed', records=records, records_fingerprint=digest(records))
    return receipt


def prepare_maintenance(dungeon, candidate_ids, plan):
    """Cover ordinary maintenance creation as well as the observed-preparation path."""
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog as Log, AdventurerS1WorldFactCandidate as Candidate
    from db_repository.s1_world_actor_identity_repository import actor_name_exists
    from plugins.adventurer_s1.services.s1_encounter_preparation_service import eligible
    rows = (Candidate.query.join(Log, Log.id == Candidate.expedition_log_id)
            .filter(Candidate.id.in_(candidate_ids), Candidate.target_dungeon == dungeon,
                    Log.status == 'completed').with_entities(Candidate.id, Log.id, Log.full_log).all())
    jobs = []
    for index, actor in enumerate(plan.get('world_actors') or []):
        if (not isinstance(actor, dict) or actor.get('actor_type') != 'individual'
                or actor.get('status') != 'active' or 'id' in actor or 'actor_id' in actor
                or actor_name_exists(actor.get('canonical_name', ''))):
            continue
        sources = [(cid, lid, full) for cid, lid, full in rows
                   if eligible(full) and full.get('settlement_receipt', {}).get('status') == 'passed'
                   and any(profile.get('scope') == 'individual'
                           and not profile.get('world_actor_id')
                           and profile.get('subject') == actor.get('canonical_name')
                           for profile in full.get('objective_outline', {}).get('encounter_capability_profiles', []))]
        if len(sources) == 1:
            cid, lid, full = sources[0]
            jobs.append((index, deepcopy(actor), cid, lid, deepcopy(full)))
        elif len(sources) > 1:
            raise FirstEncounterLearningError('first_encounter_learning.ambiguous_source')
    db_manager.remove()
    return [{**extract(full, actor, source_log_id=lid, source_candidate_id=cid),
             'maintenance_actor_index': index} for index, actor, cid, lid, full in jobs]


def _source_profile_ref(full, canonical_name, index=_SOURCE_PROFILE_INDEX_UNSET):
    """Bind a reviewed index or an exact unique name; never infer an alias."""
    profiles = (full.get('objective_outline') or {}).get('encounter_capability_profiles') or []
    if index is None:
        return None
    if index is _SOURCE_PROFILE_INDEX_UNSET:
        matches = [i for i, row in enumerate(profiles)
                   if isinstance(row, dict) and row.get('scope') == 'individual'
                   and not row.get('world_actor_id') and row.get('subject') == canonical_name]
        if len(matches) != 1:
            return None
        index = matches[0]
    if type(index) is not int or not 0 <= index < len(profiles):
        raise ValueError('first_encounter_learning.source_profile_invalid')
    profile = profiles[index]
    if (not isinstance(profile, dict) or profile.get('scope') != 'individual'
            or profile.get('world_actor_id') or not isinstance(profile.get('subject'), str)
            or not profile['subject'].strip()):
        raise ValueError('first_encounter_learning.source_profile_invalid')
    return {'profile_index': index, 'subject': profile['subject'], 'profile_fingerprint': digest(profile)}


def _complete_admission_owner(stored):
    """Completion is metadata only, bound to the same immutable source profile."""
    from .s1_countermeasure_learning_contract import COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD
    owner = stored.get(COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD)
    if not isinstance(owner, dict) or not owner.get('pending_admission_subjects'):
        return
    admissions = stored.get('_first_encounter_learning_admissions') or {}
    profiles = (stored.get('objective_outline') or {}).get('encounter_capability_profiles') or []
    fingerprint = source_fingerprint(stored)
    completed = set()
    for item in admissions.values():
        if (not isinstance(item, dict) or item.get('status') not in {'completed', 'disabled'}
                or item.get('source_fingerprint') != fingerprint):
            continue
        ref = item.get('source_profile_ref')
        if 'source_profile_ref' not in item:
            ref = _source_profile_ref(stored, (item.get('actor') or {}).get('canonical_name'))
        if ref is None:
            continue
        if not isinstance(ref, dict):
            raise ValueError('first_encounter_learning.source_profile_changed')
        expected = _source_profile_ref(stored, None, ref.get('profile_index'))
        if ref != expected:
            raise ValueError('first_encounter_learning.source_profile_changed')
        # Legacy waiting lists contain names, so duplicate frozen subjects must
        # remain unresolved rather than clearing several identities at once.
        if sum(isinstance(row, dict) and row.get('subject') == ref['subject'] for row in profiles) == 1:
            completed.add(ref['subject'])
    pending = [name for name in owner.get('pending_admission_subjects', []) if name not in completed]
    owner.update(pending_admission_subjects=pending, admitted_actor_ids=sorted(int(key) for key in admissions))
    if not pending:
        owner['admission_status'] = 'completed'
        if owner.get('status') == 'awaiting_admission':
            owner['status'] = 'completed'


def persist(receipt, actor, *, dungeon, candidate_ids, source_profile_index=_SOURCE_PROFILE_INDEX_UNSET):
    """Called in admission transaction; actor + carrier + knowledge commit together."""
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog as Log, AdventurerS1WorldFactCandidate as Candidate
    from plugins.adventurer_s1.services.s1_countermeasure_learning_contract import (
        COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD, COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION,
    )
    from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import persist_completed_learning
    from db_repository.s1_encounter_preparation_repository import ensure_observed_actor_carrier
    log = Log.query.filter_by(id=receipt['source_log_id']).with_for_update().populate_existing().first()
    candidate = Candidate.query.filter_by(id=receipt['source_candidate_id']).first()
    if (receipt.get('source_candidate_id') not in candidate_ids
            or not log or log.status != 'completed' or not candidate
            or candidate.expedition_log_id != log.id or candidate.target_dungeon != dungeon
            or source_fingerprint(log.full_log) != receipt['source_fingerprint']):
        raise ValueError('first_encounter_learning.source_changed')
    prior = ((log.full_log or {}).get('_first_encounter_learning_admissions') or {}).get(str(actor.id)) if log else None
    source_ref = (deepcopy(prior['source_profile_ref'])
                  if (source_profile_index is _SOURCE_PROFILE_INDEX_UNSET
                      and isinstance(prior, dict) and 'source_profile_ref' in prior)
                  else _source_profile_ref(log.full_log, receipt['actor']['canonical_name'], source_profile_index))
    if (isinstance(prior, dict) and receipt.get('source_candidate_id') in candidate_ids
            and prior.get('receipt_fingerprint') == digest(receipt)):
        if (prior.get('actor_id') != actor.id
                or receipt['source_candidate_id'] not in (actor.source_candidate_ids or [])):
            raise ValueError('first_encounter_learning.identity_binding_invalid')
        stored = deepcopy(log.full_log)
        admission = stored['_first_encounter_learning_admissions'][str(actor.id)]
        if (source_profile_index is None and isinstance(admission.get('source_profile_ref'), dict)):
            raise ValueError('first_encounter_learning.source_profile_changed')
        if source_ref is not None:
            if admission.get('source_profile_ref') not in (None, source_ref):
                raise ValueError('first_encounter_learning.source_profile_changed')
            admission['source_profile_ref'] = source_ref
        elif source_profile_index is None:
            admission['source_profile_ref'] = None
        _complete_admission_owner(stored)
        log.full_log = stored
        return deepcopy(prior['persistence'])
    if (receipt.get('status') not in {'completed', 'disabled'}
            or receipt.get('source_candidate_id') not in candidate_ids
            or receipt.get('actor_fingerprint') != digest(receipt.get('actor'))
            or actor.canonical_name != receipt['actor']['canonical_name']
            or receipt['source_candidate_id'] not in (actor.source_candidate_ids or [])
            or actor.capability_revision != 1
            or actor.capability_profile != receipt['actor']['capability_profile']):
        raise ValueError('first_encounter_learning.identity_binding_invalid')
    records = deepcopy(receipt['records'])
    if receipt['status'] == 'completed' and digest(records) != receipt.get('records_fingerprint'):
        raise ValueError('first_encounter_learning.records_changed')
    for row in records:
        if (row.get('observer_world_actor_id') != 1 or row.get('observer_capability_revision') != 1
                or row.get('matched_knowledge_id') is not None):
            raise ValueError('first_encounter_learning.local_identity_invalid')
        row['observer_world_actor_id'] = actor.id
    actor.first_seen_log_id = log.id
    actor.capability_source_log_id = log.id
    full = learning_context(log.full_log, receipt['actor'], actor.id)
    full['countermeasure_learning'] = records
    full[COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD] = {
        'schema_version': COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION, 'status': receipt['status'],
    }
    if 'capability_growth' in receipt:
        from .s1_countermeasure_growth_service import valid_receipt
        growth = deepcopy(receipt['capability_growth'])
        if growth.get('status') == 'reviewed':
            if not valid_receipt(growth):
                raise ValueError('first_encounter_learning.growth_changed')
            for row in growth['approved'] + growth['reviews']:
                if row['observer_world_actor_id'] != 1:
                    raise ValueError('first_encounter_learning.growth_identity_invalid')
                row['observer_world_actor_id'] = actor.id
                for support in row.get('supporting_records') or []:
                    if support['observer_world_actor_id'] != 1:
                        raise ValueError('first_encounter_learning.growth_support_identity_invalid')
                    support['observer_world_actor_id'] = actor.id
            growth['fingerprint'] = digest({k: growth[k] for k in ('approved', 'reviews', 'source_log_id')})
        full[COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD]['capability_growth'] = growth
    result = persist_completed_learning(expedition_log_id=log.id, final_result_data=full) if records else {
        'created': 0, 'updated': 0, 'encounters': 0,
    }
    if (result.get('deferred_audience_records') or result.get('validation_disposition', {}).get('quarantined_records')
            or result.get('status') == 'quarantined'):
        raise ValueError('first_encounter_learning.persistence_incomplete')
    carrier_id = ensure_observed_actor_carrier(actor, log=log, candidate=candidate, dungeon=dungeon)
    stored = deepcopy(log.full_log)
    admissions = dict(stored.get('_first_encounter_learning_admissions') or {})
    admissions[str(actor.id)] = {
        **deepcopy(receipt), 'actor_id': actor.id, 'records': records,
        'receipt_fingerprint': digest(receipt),
        'source_profile_ref': source_ref,
        'world_story_thread_id': carrier_id, 'persistence': result,
    }
    stored['_first_encounter_learning_admissions'] = admissions
    _complete_admission_owner(stored)
    log.full_log = stored
    return result
