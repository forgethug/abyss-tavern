"""Short transactions for campaign continuation jobs and exact predecessor links."""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime, timedelta
from sqlalchemy import or_, and_, func
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1CampaignContinuation as Job,
    AdventurerS1CampaignDispatch as Dispatch,
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1WorldStoryThreadActor as ActorLink,
    AdventurerS1WorldActor as Actor,
    AdventurerS1FactionProfileThread as FactionLink,
    AdventurerS1FactionProfile as Faction,
    AdventurerS1CampaignDispatchReport as Report,
    AdventurerS1ExpeditionLog as Log,
)


class S1CampaignContinuationRepository:
    @staticmethod
    def status_row(root_id):
        row = db.session.query(
            Job.input_hash, Job.status, Job.attempts, Job.error, Job.admin_user_id,
            Job.request['automatic'].as_boolean().label('automatic'),
            Job.request['reason'].as_string().label('reason'),
        ).join(Dispatch, Job.dispatch_id == Dispatch.id).filter(Dispatch.campaign_root_id == root_id).first()
        return dict(row._mapping) if row else None

    @staticmethod
    def latest_evidence_ids(dispatch_id):
        latest = db.session.query(func.max(Report.revision).label('revision')).join(
            Log, Log.id == Report.expedition_log_id).filter(
            Report.dispatch_id == dispatch_id, Log.status == 'completed').group_by(Log.target_dungeon).subquery()
        return {log_id for (log_id,) in db.session.query(Report.expedition_log_id).filter(
            Report.dispatch_id == dispatch_id, Report.revision.in_(db.select(latest.c.revision))).all()}

    @staticmethod
    def for_root(root_id):
        return Job.query.join(Dispatch, Job.dispatch_id == Dispatch.id).filter(
            Dispatch.campaign_root_id == root_id).first()

    @staticmethod
    def get(dispatch_id, *, lock=False):
        query = Job.query.filter_by(dispatch_id=dispatch_id)
        if lock:
            query = query.populate_existing().with_for_update()
        return query.first()

    @staticmethod
    def save(dispatch_id, snapshot, request, actor_id):
        row = S1CampaignContinuationRepository.get(dispatch_id, lock=True)
        if row is None:
            row = Job(dispatch_id=dispatch_id)
            db.session.add(row)
        row.input_hash, row.snapshot = snapshot['input_hash'], deepcopy(snapshot)
        row.request, row.admin_user_id = deepcopy(request), actor_id
        row.status, row.attempts = 'pending', 0
        row.proposal = row.review = row.error = row.owner_token = row.lease_until = None
        row.updated_at = _portable_utc_now()
        db.session.flush()
        return row

    @staticmethod
    def pending_roots(limit, max_attempts, retry_seconds):
        now = _portable_utc_now()
        return [root_id for (root_id,) in db.session.query(Dispatch.campaign_root_id).join(
            Job, Job.dispatch_id == Dispatch.id).filter(or_(
                Job.status == 'pending',
                and_(Job.status == 'prepared', Job.request['automatic'].as_boolean().is_(True)),
                and_(Job.status == 'processing', Job.lease_until <= now),
                and_(Job.status == 'failed', Job.attempts < max_attempts,
                     Job.updated_at <= now - timedelta(seconds=retry_seconds)),
            )).order_by(Job.updated_at, Job.dispatch_id).limit(limit).all()]

    @staticmethod
    def related_actors(front_ids, *, lock=False):
        links = ActorLink.query.filter(ActorLink.world_story_thread_id.in_(front_ids)).order_by(
            ActorLink.world_story_thread_id, ActorLink.actor_id).all() if front_ids else []
        ids = {r.actor_id for r in links}
        query = Actor.query.filter(Actor.id.in_(ids)).populate_existing().order_by(Actor.id)
        if lock:
            query = query.with_for_update(read=True, nowait=True)
        rows = query.all() if ids else []
        fields = ('id', 'canonical_name', 'status', 'identity_summary', 'power_boundary',
                  'capability_profile', 'capability_revision', 'current_state', 'current_agenda')
        return [{**{k: deepcopy(getattr(row, k)) for k in fields},
                 'thread_ids': [link.world_story_thread_id for link in links if link.actor_id == row.id]}
                for row in rows]

    @staticmethod
    def create_successors(fronts, proposals):
        by_id = {f.id: f for f in fronts}
        titles = [p['title'] for p in proposals]
        if titles and Thread.query.filter(Thread.title.in_(titles)).first():
            raise ValueError('后续事件标题已存在，请重新准备衔接')
        created = []
        for proposal in proposals:
            old = by_id[proposal['predecessor_id']]
            row = Thread(
                title=proposal['title'], summary=proposal['summary'],
                campaign_agenda=proposal['campaign_agenda'],
                predecessor_id=old.id, chain_key=old.chain_key or f'world-thread:{old.id}',
                thread_type=old.thread_type, status='seeded', base_dungeon=old.base_dungeon,
                scope_keys=deepcopy(old.scope_keys or [old.base_dungeon]),
                trigger_keywords=[], power_tier=old.power_tier, power_profile=old.power_profile,
                weight=old.weight, revision=1, is_chain_head=True,
                campaign_root_id=old.campaign_root_id, campaign_role=old.campaign_role,
                source_candidate_ids=deepcopy(old.source_candidate_ids or []),
            )
            db.session.add(row)
            created.append((old, row))
        db.session.flush()
        mapping = {old.id: row.id for old, row in created}
        if mapping:
            actors = ActorLink.query.filter(ActorLink.world_story_thread_id.in_(mapping)).all()
            factions = FactionLink.query.filter(FactionLink.world_story_thread_id.in_(mapping)).all()
            db.session.add_all([ActorLink(
                world_story_thread_id=mapping[r.world_story_thread_id], actor_id=r.actor_id,
                involvement=r.involvement, narrative_role=r.narrative_role, lease_required=r.lease_required)
                for r in actors])
            db.session.add_all([FactionLink(
                world_story_thread_id=mapping[r.world_story_thread_id], faction_profile_id=r.faction_profile_id,
                relation_role=r.relation_role, source_log_id=r.source_log_id,
                presence_debt=r.presence_debt, presence_cooldown_runs=r.presence_cooldown_runs,
                last_presence_log_id=r.last_presence_log_id) for r in factions])
        for old, row in created:
            old.is_chain_head = False
            old.superseded_by_id = row.id
            old.revision += 1
            # The terminal status, summary, and losses remain historical facts.
        db.session.flush()
        return [row for _, row in created]

    @staticmethod
    def related_factions(front_ids, *, lock=False):
        links = FactionLink.query.filter(FactionLink.world_story_thread_id.in_(front_ids)).order_by(
            FactionLink.world_story_thread_id, FactionLink.faction_profile_id).all() if front_ids else []
        ids = {r.faction_profile_id for r in links}
        query = Faction.query.filter(Faction.id.in_(ids)).populate_existing().order_by(Faction.id)
        if lock:
            query = query.with_for_update(read=True, nowait=True)
        rows = query.all() if ids else []
        fields = ('id', 'canonical_name', 'status', 'identity_summary', 'facets', 'revision')
        return [{**{k: deepcopy(getattr(row, k)) for k in fields},
                 'thread_ids': [link.world_story_thread_id for link in links if link.faction_profile_id == row.id]}
                for row in rows]
