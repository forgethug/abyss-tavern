"""Exact campaign/world identities; callers own short transactions and lock order."""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime
from sqlalchemy import func, or_

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1CampaignEvolution as Job,
    AdventurerS1CampaignDispatch as Dispatch,
    AdventurerS1CampaignDispatchClaim as Claim,
    AdventurerS1CampaignDispatchReport as Report,
    AdventurerS1CampaignStageState as Stage,
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1WorldStoryThreadActor as ActorLink,
    AdventurerS1WorldActor as Actor,
    AdventurerS1WorldLocationState as Location,
    AdventurerS1WorldOutlineDungeonState as Dungeon,
    AdventurerS1ExpeditionLog as Log,
    AdventurerS1NarrativeLease as Lease,
)


def _query(query, lock):
    return query.populate_existing().with_for_update(nowait=True) if lock else query


class S1CampaignEvolutionRepository:
    @staticmethod
    def roots(now, maximum):
        rows = db.session.query(Dispatch.campaign_root_id).join(
            Stage, Stage.dispatch_id == Dispatch.id,
        ).outerjoin(Job, Job.dispatch_id == Dispatch.id).filter(
            Dispatch.discovered_at.isnot(None), Dispatch.ended_at.is_(None),
            Stage.status == 'active', Dispatch.active_log_id.is_(None),
            Stage.plan['advance_mode'].as_string() == 'auto',
            Stage.plan['continuity_enabled'].as_boolean().is_(True),
            or_(Job.dispatch_id.is_(None), Job.next_check_at <= now),
            or_(Job.status != 'processing', Job.status.is_(None), Job.lease_until <= now),
        ).order_by(func.coalesce(Job.next_check_at, Dispatch.discovered_at), Dispatch.id).limit(maximum).all()
        return [row[0] for row in rows]

    @staticmethod
    def get(dispatch_id, *, lock=False):
        return _query(Job.query.filter_by(dispatch_id=dispatch_id), lock).first()

    @staticmethod
    def create(dispatch_id, now):
        job = Job(dispatch_id=dispatch_id, next_check_at=now)
        db.session.add(job)
        db.session.flush()
        return job

    @staticmethod
    def claimed(dispatch_id):
        return db.session.query(Claim.expedition_log_id).filter_by(dispatch_id=dispatch_id).first() is not None

    @staticmethod
    def reports(dispatch_id):
        latest = db.session.query(Log.target_dungeon.label('dungeon'),
            func.max(Report.revision).label('revision')).join(
                Report, Report.expedition_log_id == Log.id).filter(
                    Report.dispatch_id == dispatch_id, Report.published_at.isnot(None),
                    Log.status == 'completed').group_by(Log.target_dungeon).subquery()
        return [{'dungeon': dungeon, 'revision': revision,
                 'published_at': published.isoformat(), 'log_id': log_id,
                 'report': {key: deepcopy(report.get(key)) for key in (
                     'public_title', 'public_summary', 'applied_world_state', 'outcome')}}
                for dungeon, revision, published, log_id, report in db.session.query(
                    latest.c.dungeon, Report.revision, Report.published_at,
                    Report.expedition_log_id, Report.report).join(
                        Report, Report.revision == latest.c.revision).filter(
                            Report.dispatch_id == dispatch_id).order_by(latest.c.dungeon).all()]

    @staticmethod
    def world(root_id, maps, references, *, lock=False):
        """Campaign actors (including retired history), exact locations and map aggregates."""
        identity = dict((identifier, dungeon) for dungeon, identifier in references)
        all_refs = db.session.query(Location.id, Location.target_dungeon).filter(Location.id.in_(identity)).all()
        if dict(all_refs) != identity or any(dungeon not in maps for dungeon in identity.values()):
            raise ValueError('战役演进的地点身份缺失或跨地图')
        locations = _query(Location.query.filter(
            Location.target_dungeon.in_(maps), Location.current_state != '', or_(
                Location.id.in_(identity), Location.canonical_name == Location.target_dungeon),
        ).order_by(Location.id), lock).all()
        actor_ids = db.session.query(ActorLink.actor_id).join(
            Thread, Thread.id == ActorLink.world_story_thread_id).filter(or_(
                Thread.id == root_id, Thread.campaign_root_id == root_id))
        actors = _query(Actor.query.filter(Actor.id.in_(actor_ids)).order_by(Actor.id), lock).all()
        from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository as Dispatches
        # Lease acquisition locks the same actor row before inserting its lease.
        occupied = {row[0] for row in db.session.query(Lease.world_actor_id).join(
            Log, Log.id == Lease.expedition_log_id).filter(
                Lease.world_actor_id.in_([row.id for row in actors]), Lease.lease_type == 'world_actor',
                Lease.status == 'active', or_(Log.status.in_(('generating', 'in_progress')),
                    Dispatches.ownership_exists(Log.id))).distinct().all()}
        dungeons = _query(Dungeon.query.filter(Dungeon.target_dungeon.in_(maps)).order_by(Dungeon.id), lock).all()
        explorable = {row.target_dungeon for row in dungeons if row.is_explorable is True}
        def fields(row, names):
            return {name: deepcopy(getattr(row, name)) for name in names}
        now = _portable_utc_now()
        return {
            'locations': [dict(fields(row, ('id', 'target_dungeon', 'canonical_name', 'revision',
                    'current_state', 'public_risk_notice')),
                    state_updated_at=row.updated_at.isoformat() + 'Z',
                    write_available=row.target_dungeon in explorable and not (
                        row.write_lease_log_id is not None and row.write_lease_expires_at
                        and row.write_lease_expires_at > now)) for row in locations],
            'actors': [dict(fields(row, ('id', 'canonical_name', 'status', 'identity_summary', 'stable_principles',
                'power_boundary', 'capability_profile', 'capability_revision', 'long_term_goal',
                'current_state', 'current_agenda', 'scope_keys')),
                state_updated_at=row.updated_at.isoformat() + 'Z', write_available=row.id not in occupied) for row in actors],
            'dungeons': [fields(row, ('target_dungeon', 'is_explorable', 'outline_revision')) for row in dungeons],
        }, {row.id: row for row in actors}

    @staticmethod
    def public_notices(dispatch_ids):
        return {identifier: {'title': notice['title'], 'summary': notice['summary'],
                            'occurred_at': timestamp}
                for identifier, notice, timestamp in db.session.query(
                    Job.dispatch_id, Job.public_notice, Job.public_notice_at).filter(
                        Job.dispatch_id.in_(dispatch_ids), Job.public_notice.isnot(None)).all()
                if isinstance(notice, dict) and isinstance(notice.get('title'), str)
                and isinstance(notice.get('summary'), str) and timestamp is not None}

    @staticmethod
    def departure_state(dispatch_id, dungeon):
        row = db.session.query(Job.world_states).filter_by(dispatch_id=dispatch_id).first()
        value = (row[0] or {}).get(dungeon) if row else None
        if not value:
            return None
        latest = db.session.query(func.max(Report.revision)).join(
            Log, Log.id == Report.expedition_log_id).filter(
                Report.dispatch_id == dispatch_id, Log.target_dungeon == dungeon).scalar() or 0
        if latest > value['basis_report_revision']:
            return None
        # Subsequent ordinary world maintenance may update the same head without
        # publishing a campaign report. Its current summary outranks this old job.
        head = db.session.query(Thread.summary).join(Dispatch, Dispatch.campaign_root_id == Thread.campaign_root_id).filter(
            Dispatch.id == dispatch_id, Thread.id == value['thread_id'], Thread.base_dungeon == dungeon,
            or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None))).first()
        return head[0] if head else None
