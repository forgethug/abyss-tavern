"""Timer -> frozen facts -> proposal/review -> atomic existing-world writeback."""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime, timedelta
import threading
from uuid import uuid4
from flask import current_app

from db_repository import db_manager
from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository as Stages
from db_repository.s1_campaign_evolution_repository import S1CampaignEvolutionRepository as Repo
from plugins.adventurer_s1.services.s1_campaign_stage_service import _identity, _snapshot
from plugins.adventurer_s1.services.s1_campaign_stage_contract import stage_definition
from plugins.adventurer_s1.services.s1_campaign_continuation_service import _location_refs
from plugins.adventurer_s1.services.s1_campaign_evolution_contract import (
    config, digest, normalize_proposal, normalize_review,
)


def _interval():
    return timedelta(hours=int(config('IDLE_HOURS')))


def _owned(job, token):
    return bool(job and job.status == 'processing' and job.owner_token == token
                and job.lease_until and job.lease_until > _portable_utc_now())


def _available(dispatch, state, fronts):
    if (not config('ENABLED') or not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True)
            or not dispatch or not state or not dispatch.discovered_at or dispatch.ended_at
            or state.status != 'active' or state.plan.get('advance_mode') != 'auto'
            or state.plan.get('continuity_enabled') is not True):
        return False
    return not (dispatch.active_log_id or Repo.claimed(dispatch.id) or Stages.busy([f.id for f in fronts]))


def _freeze(dispatch, state, fronts, job, at, *, lock=False):
    source = _identity()
    reports = Repo.reports(dispatch.id)
    anchor = max([dispatch.discovered_at, *([job.checked_at] if job.checked_at else []),
                  *(datetime.fromisoformat(r['published_at']) for r in reports)])
    if at < anchor + _interval():
        return None, anchor + _interval(), {}
    maps = sorted({f.base_dungeon for f in fronts if f.campaign_role == 'dungeon_front' and f.base_dungeon})
    world, actors = Repo.world(dispatch.campaign_root_id, maps, _location_refs(dispatch), lock=lock)
    root = next(f for f in fronts if f.id == dispatch.campaign_root_id)
    stage = stage_definition(state.plan, state.stage_key)
    frozen = {
        'source': source, 'before': _snapshot(dispatch, state, fronts, source),
        'clock': {'campaign_started_at': root.created_at.isoformat(),
                  'campaign_discovered_at': dispatch.discovered_at.isoformat(),
                  'anchor_at': anchor.isoformat(), 'as_of': at.isoformat(),
                  'elapsed_seconds': int((at - anchor).total_seconds())},
        'campaign_world': deepcopy(root.campaign_world),
        'stage': {**{key: deepcopy(stage[key]) for key in (
            'key', 'title', 'public_title', 'nodes', 'completion_nodes') if key in stage},
            'has_failure_exit': isinstance(stage.get('failure_next'), str)},
        'progress': deepcopy(state.progress), 'public_summary': dispatch.public_summary,
        'fronts': [{key: deepcopy(getattr(f, key)) for key in (
            'id', 'campaign_role', 'base_dungeon', 'status', 'revision', 'title', 'summary',
            'scope_keys', 'power_profile', 'campaign_agenda')} for f in fronts],
        'reports': reports, 'world': world,
    }
    frozen['input_hash'] = digest(frozen)
    return frozen, anchor + _interval(), actors


def _heartbeat(app, dispatch_id, token, stop):
    with app.app_context():
        while not stop.wait(int(config('HEARTBEAT_SECONDS'))):
            try:
                with db_manager.transaction():
                    job = Repo.get(dispatch_id, lock=True)
                    if not _owned(job, token):
                        return
                    job.lease_until = _portable_utc_now() + timedelta(seconds=int(config('LEASE_SECONDS')))
            except Exception:
                current_app.logger.exception('S1 战役演进续租失败 dispatch=%s', dispatch_id)
            finally:
                db_manager.remove()


def _apply(dispatch, state, fronts, job, snapshot, proposal, review, actors):
    """The caller holds fresh campaign/world locks and the live job token."""
    if review['decision'] != 'allow':
        raise ValueError('campaign_evolution_review_rejected')
    key = 'evolution:' + snapshot['input_hash'][:54]
    if Stages.receipt(key):
        return
    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
    locations = {r['id']: r for r in snapshot['world']['locations']}
    by_map = {}
    for change in proposal['location_updates']:
        by_map.setdefault(locations[change['location_id']]['target_dungeon'], []).append(change)
    applied = []
    for dungeon, changes in sorted(by_map.items()):
        basis = next((r for r in snapshot['world']['dungeons'] if r['target_dungeon'] == dungeon), None)
        if not basis or basis['is_explorable'] is not True:
            raise ValueError('campaign_evolution_dungeon_not_explorable')
        frozen = {'target_dungeon': dungeon, 'is_explorable': True, 'locations': [
            {'location_id': r['id'], 'location_name': r['canonical_name'], 'revision': r['revision'],
             'current_state': r['current_state'], '_write_available': r['write_available']}
            for r in locations.values() if r['target_dungeon'] == dungeon]}
        applied.extend(S1WorldLocationStateService.apply_maintenance_location_changes(
            dungeon, changes, snapshot=frozen, expected_outline_revision=basis['outline_revision']))
    now = _portable_utc_now()
    for change in proposal['actor_updates']:
        row = actors[change['actor_id']]
        row.current_state, row.current_agenda = change['current_state'], change['current_agenda']
        row.updated_at = now
    heads = {f.id: f for f in fronts}
    world_states = deepcopy(job.world_states or {})
    revisions = {r['dungeon']: r['revision'] for r in snapshot['reports']}
    for change in proposal['front_updates']:
        row = heads[change['thread_id']]
        row.summary, row.updated_at = change['summary'], now
        row.revision = int(row.revision or 1) + 1
        # This private current-state projection bridges older revelation inputs.
        world_states[row.base_dungeon] = {'state': change['summary'],
            'thread_id': row.id,
            'basis_report_revision': revisions.get(row.base_dungeon, 0)}
    job.world_states = world_states
    if proposal['task_updates']:
        plan = deepcopy(state.plan)
        nodes = {n['key']: n for n in stage_definition(plan, state.stage_key)['nodes']}
        for change in proposal['task_updates']:
            nodes[change['node_key']].update(title=change['title'], objective=change['objective'])
        state.plan, state.updated_at = plan, now
    notice = proposal['public_notice']
    if notice:
        job.public_notice, job.public_notice_at = notice, now
    if proposal['disposition'] == 'changed':
        dispatch.updated_at = now
    # Task identity/maps/goals remain unchanged; keep accepted forming tickets valid.
    Stages.add_receipt(dispatch_id=dispatch.id, operation_key=key, kind='world_evolution',
        admin_user_id=0, reason=proposal['reason'], before=snapshot,
        after={'source': snapshot['source'], 'input_hash': snapshot['input_hash'],
               'proposal': proposal, 'review': review, 'applied_locations': applied})


class S1CampaignEvolutionService:
    @staticmethod
    def submit():
        if not config('ENABLED') or not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True):
            return False
        from services.async_task_service import AsyncTaskService
        return AsyncTaskService.submit_task('ai_s1_campaign_evolution', {})

    @staticmethod
    def process_pending():
        if not config('ENABLED') or not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True):
            return
        if not 0 < int(config('HEARTBEAT_SECONDS')) < int(config('LEASE_SECONDS')) or int(config('IDLE_HOURS')) < 1:
            raise ValueError('战役演进时钟或租约配置无效')
        try:
            with db_manager.transaction():
                _identity()
                roots = Repo.roots(_portable_utc_now(), int(config('BATCH_SIZE')))
            db_manager.remove()
            for root_id in roots:
                S1CampaignEvolutionService.process(root_id)
        finally:
            db_manager.remove()

    @staticmethod
    def process(root_id):
        token, dispatch_id = uuid4().hex, None
        stop, heartbeat = threading.Event(), None
        failure_kind, proposal, review = 'consumer', None, None
        try:
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                if not _available(dispatch, state, fronts):
                    return
                dispatch_id = dispatch.id
                now = _portable_utc_now()
                job = Repo.get(dispatch_id, lock=True) or Repo.create(dispatch_id, now)
                if job.next_check_at > now or (job.status == 'processing' and job.lease_until and job.lease_until > now):
                    return
                if job.status == 'processing':
                    # A crashed worker has no completed failure receipt. Unknown
                    # physical-call outcome uses its own bounded recovery budget.
                    job.interruptions += 1
                    if job.interruptions >= int(config('MAX_INTERRUPTS')):
                        job.status, job.error = 'interrupted', '演进作业中断恢复预算已耗尽，次日再核验'
                        job.owner_token = job.lease_until = None
                        job.next_check_at, job.updated_at = now + _interval(), now
                        return
                elif job.status == 'interrupted':
                    job.interruptions = 0
                if job.consumer_failures >= int(config('MAX_CONSUMER_FAILURES')):
                    job.consumer_failures = 0  # a new daily consumer retry window
                # Exhausted work starts a new daily window, never a tight retry loop.
                old = job.snapshot if job.status in ('failed', 'processing', 'interrupted') and job.attempts < int(config('MAX_ATTEMPTS')) else None
                at = datetime.fromisoformat(old['clock']['as_of']) if old else now
                snapshot, due, _ = _freeze(dispatch, state, fronts, job, at)
                if snapshot is None:
                    job.status, job.next_check_at = 'waiting', due
                    job.owner_token = job.lease_until = None
                    return
                if not old or old['input_hash'] != snapshot['input_hash']:
                    snapshot, _, _ = _freeze(dispatch, state, fronts, job, now)
                    job.attempts, job.proposal, job.review = 0, None, None
                    job.interruptions, job.consumer_failures = 0, 0
                job.snapshot = snapshot
                job.status, job.owner_token = 'processing', token
                job.lease_until = now + timedelta(seconds=int(config('LEASE_SECONDS')))
                job.phase = 'consumer' if job.review else 'review' if job.proposal else 'plan'
                job.updated_at, job.error = now, None
                snapshot, previous, attempt = deepcopy(snapshot), deepcopy(job.proposal), job.attempts + 1
                previous_review = deepcopy(job.review)
            db_manager.remove()
            heartbeat = threading.Thread(target=_heartbeat,
                args=(current_app._get_current_object(), dispatch_id, token, stop), daemon=True)
            heartbeat.start()
            from plugins.adventurer_s1.services import s1_campaign_evolution_ai_service as AI
            failure_kind = 'model'
            proposal = normalize_proposal(previous or AI.call(snapshot, attempt=attempt), snapshot)
            failure_kind = 'consumer'
            with db_manager.transaction():
                job = Repo.get(dispatch_id, lock=True)
                if not _owned(job, token):
                    return
                job.proposal = proposal
                job.phase = 'consumer' if previous_review else 'review'
            db_manager.remove()
            failure_kind = 'model'
            review = normalize_review(previous_review or AI.call(snapshot, proposal, attempt=attempt), snapshot, proposal)
            failure_kind = 'consumer'
            # A consumer/SQL conflict must retry this exact reviewed artifact, not
            # regenerate the world or pay for a second successful semantic review.
            with db_manager.transaction():
                job = Repo.get(dispatch_id, lock=True)
                if not _owned(job, token):
                    return
                job.review = review
                job.phase = 'consumer'
            db_manager.remove()
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                job = Repo.get(dispatch_id, lock=True)
                if not _owned(job, token):
                    return
                if not _available(dispatch, state, fronts):
                    job.status, job.error = 'stale', '核验期间已有小队或世界事实推进'
                else:
                    fresh, _, actors = _freeze(dispatch, state, fronts, job,
                        datetime.fromisoformat(snapshot['clock']['as_of']), lock=True)
                    if not fresh or fresh['input_hash'] != snapshot['input_hash']:
                        job.status, job.error = 'stale', '核验期间战役或地点/人物版本已变化'
                    elif review['decision'] == 'reject':
                        job.status, job.error = 'rejected', review['reason']
                    else:
                        _apply(dispatch, state, fronts, job, fresh, proposal, review, actors)
                        job.status, job.checked_at, job.error = 'applied', _portable_utc_now(), None
                job.review = review
                job.owner_token = job.lease_until = None
                job.next_check_at = (job.checked_at + _interval() if job.status == 'applied'
                    else _portable_utc_now() + (_interval() if job.status == 'rejected'
                        else timedelta(seconds=int(config('RETRY_SECONDS')))))
                job.updated_at = _portable_utc_now()
                outcome = job.status
            current_app.logger.info('S1_CAMPAIGN_EVOLUTION_RESULT root_id=%s disposition=%s decision=%s status=%s',
                                    root_id, proposal['disposition'], review['decision'], outcome)
        except Exception as exc:
            current_app.logger.exception('S1 战役定时演进失败 root_id=%s', root_id)
            db_manager.remove()
            if dispatch_id is not None:
                with db_manager.transaction():
                    job = Repo.get(dispatch_id, lock=True)
                    if _owned(job, token):
                        if failure_kind == 'consumer':
                            job.consumer_failures += 1
                            # The receipt transaction may have failed after the
                            # model returned. Preserve the locally validated result
                            # in this recovery transaction if SQL is available now.
                            if proposal is not None:
                                job.proposal = proposal
                            if review is not None:
                                job.review = review
                            job.phase = 'consumer' if job.review else 'review' if job.proposal else 'plan'
                        else:
                            job.attempts += 1
                        job.status, job.error = 'failed', str(exc)[:int(config('TEXT_LIMIT'))]
                        job.owner_token = job.lease_until = None
                        job.updated_at = _portable_utc_now()
                        job.next_check_at = job.updated_at + (_interval() if (
                            job.attempts >= int(config('MAX_ATTEMPTS'))
                            or job.consumer_failures >= int(config('MAX_CONSUMER_FAILURES')))
                            else timedelta(seconds=int(config('RETRY_SECONDS'))))
        finally:
            stop.set()
            if heartbeat:
                heartbeat.join(timeout=int(config('HEARTBEAT_JOIN_SECONDS')))
            db_manager.remove()
