"""Persistence for stage progress and immutable manual/automatic receipts."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from sqlalchemy import or_, select

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1CampaignStageState as State,
    AdventurerS1CampaignStageOperation as Operation,
    AdventurerS1CampaignDispatch as Dispatch,
    AdventurerS1CampaignDispatchReport as Report,
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1ExpeditionLog as Log,
    AdventurerS1NarrativeLease as Lease,
    AdventurerS1WorldFactCandidate as Candidate,
    AdventurerS1DeploymentIdentity as Identity,
)


class S1CampaignStageRepository:
    @staticmethod
    def identity():
        row = Identity.query.filter_by(id=1).first()
        return ({'realm_code': row.realm_code, 'deployment_id': row.deployment_id,
                 'schema_revision': row.schema_revision} if row else None)

    @staticmethod
    def states(dispatch_ids, *, lock=False):
        if not dispatch_ids:
            return {}
        query = State.query.filter(State.dispatch_id.in_(dispatch_ids)).order_by(State.dispatch_id)
        if lock:
            query = query.populate_existing().with_for_update()
        return {row.dispatch_id: row for row in query.all()}

    @staticmethod
    def ready_roots(limit):
        return [root_id for (root_id,) in db.session.query(Dispatch.campaign_root_id).join(
            State, State.dispatch_id == Dispatch.id).filter(
            State.status == 'ready', State.plan['advance_mode'].as_string() == 'auto',
            Dispatch.discovered_at.isnot(None), Dispatch.ended_at.is_(None),
            Dispatch.active_log_id.is_(None),
        ).order_by(State.updated_at, State.dispatch_id).limit(limit).all()]

    @staticmethod
    def create(dispatch, plan, *, revealed=False):
        from plugins.adventurer_s1.services.s1_campaign_stage_contract import initial_progress
        stage = plan['stages'][0]
        row = State(dispatch_id=dispatch.id, plan=plan, stage_key=stage['key'],
                    stage_instance=1, progress=initial_progress(stage, revealed=revealed), status='active')
        db.session.add(row)
        dispatch.target_dungeons = [node['dungeon'] for node in stage['nodes']]
        db.session.flush()
        return row

    @staticmethod
    def bundle(root_id, *, lock=False):
        # Same ordering as departure / world maintenance: threads, dispatch, state.
        query = Thread.query.filter(or_(Thread.id == root_id, Thread.campaign_root_id == root_id),
                                    or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None)))
        if lock:
            query = query.populate_existing().order_by(Thread.id).with_for_update()
        fronts = query.all()
        query = Dispatch.query.filter_by(campaign_root_id=root_id)
        if lock:
            query = query.populate_existing().with_for_update()
        dispatch = query.first()
        state = S1CampaignStageRepository.states([dispatch.id], lock=lock).get(dispatch.id) if dispatch else None
        return dispatch, state, fronts

    @staticmethod
    def busy(front_ids):
        if not front_ids:
            return False
        # Failed publication is terminal, not pending world work. Its released
        # historical lease must not block handoff forever. Active leases still
        # block here; callers separately guard the dispatch's current owner.
        return db.session.query(Lease.id).outerjoin(
            Candidate, Candidate.expedition_log_id == Lease.expedition_log_id,
        ).filter(Lease.world_story_thread_id.in_(front_ids),
                 or_(Lease.status == 'active', Candidate.status.in_(
                     ('awaiting_completion', 'pending', 'processing')))).first() is not None

    @staticmethod
    def evidences(dispatch_id, log_ids):
        if not log_ids:
            return {}
        return {r.expedition_log_id: (r, log) for r, log in db.session.query(Report, Log).join(
            Log, Log.id == Report.expedition_log_id).filter(
            Report.dispatch_id == dispatch_id, Report.expedition_log_id.in_(log_ids),
            Log.status == 'completed').all()}

    @staticmethod
    def receipt(operation_key):
        return Operation.query.filter_by(operation_key=operation_key).first()

    @staticmethod
    def public_ending_evidence(dispatch_id, log_ids):
        """Bounded scalar projection: never load expedition bodies or private facts."""
        return db.session.query(
            Report.expedition_log_id, Report.revision, Report.party_name, Report.published_at,
            Report.report['public_title'].as_string().label('public_title'),
            Report.report['public_summary'].as_string().label('public_summary'),
            Report.report['outcome'].as_string().label('outcome'),
        ).join(Log, Log.id == Report.expedition_log_id).filter(
            Report.dispatch_id == dispatch_id, Report.expedition_log_id.in_(log_ids),
            Report.published_at.isnot(None), Log.status == 'completed',
        ).order_by(Report.revision, Report.expedition_log_id).all()

    @staticmethod
    def entered_stage_keys(dispatch_id, current_stage_key, initial_stage_key):
        # DISTINCT bounds the result by configured stage identities, not receipt count.
        rows = db.session.execute(S1CampaignStageRepository.entered_stage_keys_statement(dispatch_id)).all()
        return {current_stage_key, initial_stage_key, *(key for (key,) in rows if key)}

    @staticmethod
    def entered_stage_keys_statement(dispatch_id):
        return select(Operation.after['stage_key'].as_string()).where(
            Operation.dispatch_id == dispatch_id,
            Operation.kind.in_(('transition', 'auto_transition', 'continuation')),
        ).distinct()

    @staticmethod
    def add_receipt(**values):
        row = Operation(**values)
        db.session.add(row)
        db.session.flush()
        return row

    @staticmethod
    def restore_front(front):
        """Caller holds the campaign bundle locks and has revalidated its preview."""
        front.status = 'active'
        front.closure_debt = 0
        front.revision = int(front.revision or 1) + 1
        front.updated_at = _portable_utc_now()

    @staticmethod
    def touch(state):
        state.updated_at = _portable_utc_now()
