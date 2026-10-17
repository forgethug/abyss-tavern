"""Database boundary for campaign invitations; callers own the outer transaction."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from sqlalchemy import func, or_

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1BuildingInstance, AdventurerS1BuildingTemplate,
    AdventurerS1CampaignDispatch as Dispatch,
    AdventurerS1CampaignDispatchClaim as Claim,
    AdventurerS1CampaignDispatchReport as Report,
    AdventurerS1CampaignStageState as Stage,
    AdventurerS1ExpeditionLog as Log, AdventurerS1MatchTicket as Ticket,
    AdventurerS1NarrativeLease as Lease,
    AdventurerS1WorldFactCandidate as Candidate,
    AdventurerS1WorldStoryThread as Thread,
)

# Read legacy recovery receipts already queued before publication failures began
# releasing ownership. No new automatic publication recovery uses this marker.
CAMPAIGN_PUBLICATION_RECOVERY_KIND = 'campaign_publication_location'


class S1CampaignDispatchRepository:
    @staticmethod
    def hall_built():
        return db.session.query(AdventurerS1BuildingInstance.id).join(
            AdventurerS1BuildingTemplate,
            AdventurerS1BuildingInstance.template_id == AdventurerS1BuildingTemplate.id,
        ).filter(AdventurerS1BuildingTemplate.code == 'campaign_hall',
                 AdventurerS1BuildingInstance.status == 'built').first() is not None

    @staticmethod
    def enable(root_id):
        root = Thread.query.populate_existing().filter_by(id=root_id).with_for_update().first()
        if not root or root.campaign_role != 'spine':
            return None, '共同战役不存在'
        existing = Dispatch.query.filter_by(campaign_root_id=root_id).first()
        if existing:
            return existing, None
        # Freeze every existing front while changing its dispatch mode. A
        # previously selected legacy expedition must recheck after this lock.
        fronts = Thread.query.populate_existing().filter(
            Thread.campaign_root_id == root_id,
            Thread.campaign_role.in_(('faction_front', 'dungeon_front')),
        ).order_by(Thread.id).with_for_update().all()
        if fronts and db.session.query(Lease.id).outerjoin(
            Candidate, Candidate.expedition_log_id == Lease.expedition_log_id,
        ).filter(Lease.world_story_thread_id.in_([front.id for front in fronts]),
                 or_(Lease.status == 'active', Candidate.status.in_(
                     ('awaiting_completion', 'pending', 'processing', 'failed')))).first():
            return None, '前线仍有探险或待处理世界事实，请完成处理后启用议事厅'
        maps = sorted({front.base_dungeon for front in fronts if front.base_dungeon
                       and front.is_chain_head is not False
                       and front.status in ('seeded', 'active', 'escalated', 'closing')})
        if not maps:
            return None, '共同战役没有可用地图'
        row = Dispatch(campaign_root_id=root_id, objective=root.campaign_agenda or '', target_dungeons=maps,
                       max_active_teams=(root.campaign_world or {}).get('max_active_teams', 1))
        db.session.add(row)
        db.session.flush()
        return row, None

    @staticmethod
    def get(dispatch_id, *, lock=False):
        query = Dispatch.query.filter_by(id=dispatch_id)
        if lock:
            query = query.populate_existing().with_for_update()
        return query.first()

    @staticmethod
    def public_page(page, per_page):
        query = Dispatch.query.filter(Dispatch.discovered_at.isnot(None))
        return query.count(), query.order_by(Dispatch.updated_at.desc(), Dispatch.id.desc()).offset(
            (page - 1) * per_page).limit(per_page).all()

    @staticmethod
    def report_page(dispatch_id, page, per_page, *, chronological=False):
        query = Report.query.filter_by(dispatch_id=dispatch_id)
        order = Report.revision.asc() if chronological else Report.revision.desc()
        return query.count(), query.order_by(order).offset(
            (page - 1) * per_page).limit(per_page).all()

    @staticmethod
    def latest_reports_by_dungeon(dispatch_id):
        """At most one published continuity record per configured campaign map."""
        latest = db.session.query(
            Log.target_dungeon.label('dungeon'), func.max(Report.revision).label('revision'),
        ).join(Report, Report.expedition_log_id == Log.id).filter(
            Report.dispatch_id == dispatch_id,
        ).group_by(Log.target_dungeon).subquery()
        return {dungeon: report for dungeon, report in db.session.query(
            latest.c.dungeon, Report.report,
        ).join(Report, Report.revision == latest.c.revision).filter(
            Report.dispatch_id == dispatch_id,
        ).all()}

    @staticmethod
    def published_dungeons(dispatch_ids):
        """Visited public maps remain entry points after their core task ends."""
        result = {identifier: set() for identifier in dispatch_ids}
        if dispatch_ids:
            for identifier, name in db.session.query(Report.dispatch_id, Log.target_dungeon).join(
                    Log, Log.id == Report.expedition_log_id).filter(
                    Report.dispatch_id.in_(dispatch_ids), Report.published_at.isnot(None)).distinct().all():
                result[identifier].add(name)
        return result

    @staticmethod
    def accepted_for_party(party_id):
        return Ticket.query.filter(Ticket.matched_party_id == party_id,
                                   Ticket.campaign_dispatch_id.isnot(None)).first()

    @staticmethod
    def candidates(dungeon, dispatch_id=None, *, limit):
        query = db.session.query(Thread, Dispatch).join(
            Dispatch, Dispatch.campaign_root_id == Thread.campaign_root_id,
        ).outerjoin(Log, Log.id == Dispatch.active_log_id).filter(Thread.base_dungeon == dungeon,
                 Thread.campaign_role.in_(('faction_front', 'dungeon_front')),
                 or_(Thread.status.in_(('seeded', 'active', 'escalated', 'closing')),
                     (Thread.status == 'dormant') & db.session.query(Stage.dispatch_id).filter(
                         Stage.dispatch_id == Dispatch.id, Stage.status == 'active').exists()),
                 or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None)),
                 Dispatch.ended_at.is_(None),
                 or_(Dispatch.max_active_teams > 1, Dispatch.active_log_id.is_(None), Log.status == 'failed'))
        query = query.filter(Dispatch.id == dispatch_id) if dispatch_id else query.filter(
            or_(Dispatch.discovered_at.is_(None),
                db.session.query(Stage.dispatch_id).filter(
                    Stage.dispatch_id == Dispatch.id, Stage.status == 'active').exists()))
        return query.order_by(Dispatch.updated_at.asc(), Dispatch.id.asc(),
                              Thread.weight.desc(), Thread.id.asc()).limit(limit).all()

    @staticmethod
    def managed_roots(root_ids):
        if not root_ids:
            return set()
        return {value for (value,) in db.session.query(Dispatch.campaign_root_id).filter(
            Dispatch.campaign_root_id.in_(root_ids)).all()}

    @staticmethod
    def unfinished_roots(root_ids):
        """Bounded scalar read; discovery and stage readiness do not end a campaign."""
        if not root_ids:
            return set()
        return {value for (value,) in db.session.query(Dispatch.campaign_root_id).filter(
            Dispatch.campaign_root_id.in_(root_ids), Dispatch.ended_at.is_(None)).all()}

    @staticmethod
    def front_access_allowed(root_id, log_id, claim_dispatch_id=None):
        if not root_id:
            return True
        row = Dispatch.query.filter_by(campaign_root_id=root_id).first()
        return not row or row.id == claim_dispatch_id or S1CampaignDispatchRepository.owns_log(row, log_id)

    @staticmethod
    def log(log_id):
        return Log.query.filter_by(id=log_id).first()

    @staticmethod
    def source_statuses(thread_ids):
        if not thread_ids:
            return {}
        return dict(db.session.query(Thread.id, Thread.status).filter(Thread.id.in_(thread_ids)).all())

    @staticmethod
    def claim(thread, dispatch_id, log, *, expected_revision, acquire, allow_hidden=False):
        # The existing bundle owns the thread -> actor lock order. The additional
        # campaign owner is locked last, also after world writes in maintenance.
        # Rollback the savepoint on a lost cross-map race, including provisional leases.
        with db.session.begin_nested() as savepoint:
            source_revision = int(thread.revision or 1)
            current = Thread.query.populate_existing().filter_by(id=thread.id).with_for_update().first()
            if (not current or int(current.revision or 1) != source_revision
                    or current.is_chain_head is False
                    or current.status not in ('seeded', 'active', 'escalated', 'closing', 'dormant')):
                savepoint.rollback()
                return None
            if not acquire():
                savepoint.rollback()
                return None
            row = S1CampaignDispatchRepository.get(dispatch_id, lock=True)
            if (not row or row.ended_at or row.revision != expected_revision
                    or row.campaign_root_id != current.campaign_root_id):
                savepoint.rollback()
                return None
            claims = Claim.query.filter_by(dispatch_id=row.id).order_by(Claim.expedition_log_id).all()
            if any(c.expedition_log_id == log.id for c in claims):
                return row
            if row.active_log_id and not claims:
                owner = Log.query.filter_by(id=row.active_log_id).first()
                # No TTL may free an expedition still generating, running or awaiting
                # world maintenance. Only an explicit technical terminal failure releases it.
                if not owner or owner.status != 'failed':
                    savepoint.rollback()
                    return None
                row.active_log_id = None
            stage = Stage.query.populate_existing().filter_by(dispatch_id=row.id).with_for_update().first()
            if current.status == 'dormant' and not stage:
                savepoint.rollback()
                return None
            if stage:
                from plugins.adventurer_s1.services.s1_campaign_stage_contract import stage_definition
                definition = stage_definition(stage.plan, stage.stage_key)
                node = next((n for n in definition['nodes'] if n['dungeon'] == log.target_dungeon), None)
                if (stage.status != 'active' or not node
                        or stage.progress[node['key']]['result'] != 'pending'
                        or (stage.progress[node['key']]['knowledge'] == 'hidden' and not allow_hidden)):
                    savepoint.rollback()
                    return None
            if (len(claims) >= row.max_active_teams
                    or any(c.target_dungeon == log.target_dungeon for c in claims)):
                savepoint.rollback()
                return None
            db.session.add(Claim(expedition_log_id=log.id, dispatch_id=row.id, target_dungeon=log.target_dungeon))
            row.active_log_id = row.active_log_id or log.id
            db.session.flush()
            return row

    @staticmethod
    def ownership_exists(log_id):
        """SQL predicate also covers serial owners created before the claim migration."""
        return or_(db.session.query(Claim.expedition_log_id).filter(
            Claim.expedition_log_id == log_id).exists(), db.session.query(Dispatch.id).filter(
            Dispatch.active_log_id == log_id).exists())

    @staticmethod
    def owns_log(row, log_id):
        return (row.active_log_id == log_id or Claim.query.filter_by(
            dispatch_id=row.id, expedition_log_id=log_id).first() is not None)

    @staticmethod
    def occupancy(dispatch_ids):
        result = {identifier: [] for identifier in dispatch_ids}
        if dispatch_ids:
            for identifier, name in db.session.query(Claim.dispatch_id, Claim.target_dungeon).filter(
                    Claim.dispatch_id.in_(dispatch_ids)).order_by(Claim.dispatch_id, Claim.target_dungeon).all():
                result[identifier].append(name)
        return result

    @staticmethod
    def occupancy_log_ids(dispatch_ids):
        result = {identifier: set() for identifier in dispatch_ids}
        if dispatch_ids:
            for identifier, log_id in db.session.query(Claim.dispatch_id, Claim.expedition_log_id).filter(
                    Claim.dispatch_id.in_(dispatch_ids)).all():
                result[identifier].add(log_id)
        return result

    @staticmethod
    def release_owners(rows, log_ids, *, now):
        """Dispatch rows must already be locked. Release only the supplied exact owners."""
        log_ids = set(log_ids)
        if not rows or not log_ids:
            return 0
        dispatch_ids = [row.id for row in rows]
        claims = Claim.query.filter(Claim.dispatch_id.in_(dispatch_ids),
                                    Claim.expedition_log_id.in_(log_ids)).all()
        released = {claim.expedition_log_id for claim in claims}
        released.update(row.active_log_id for row in rows if row.active_log_id in log_ids)
        Claim.query.filter(Claim.dispatch_id.in_(dispatch_ids), Claim.expedition_log_id.in_(log_ids)).delete(
            synchronize_session=False)
        remaining = {}
        for identifier, owner in db.session.query(Claim.dispatch_id, Claim.expedition_log_id).filter(
                Claim.dispatch_id.in_(dispatch_ids)).order_by(Claim.expedition_log_id).all():
            remaining.setdefault(identifier, owner)
        for row in rows:
            if row.active_log_id in log_ids:
                row.active_log_id = remaining.get(row.id)
                row.updated_at = now
        if released:
            Lease.query.filter(Lease.expedition_log_id.in_(released), Lease.status == 'active').update(
                {Lease.status: 'released', Lease.released_at: now, Lease.updated_at: now},
                synchronize_session=False)
        return len(released)

    @staticmethod
    def next_report_revision(row):
        if row.max_active_teams <= 1:
            return row.revision
        return (db.session.query(func.max(Report.revision)).filter(
            Report.dispatch_id == row.id).scalar() or 0) + 1

    @staticmethod
    def allowed_dungeons(root_id):
        return sorted({name for (name,) in db.session.query(Thread.base_dungeon).filter(
            Thread.campaign_root_id == root_id,
            Thread.campaign_role.in_(('faction_front', 'dungeon_front')),
            Thread.status.in_(('seeded', 'active', 'escalated', 'closing')),
            or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None)),
        ).distinct().all() if name})

    @staticmethod
    def active_maps_by_root(root_ids, *, investigation_maps=None):
        result = {}
        if root_ids:
            for root_id, name, status in db.session.query(
                Thread.campaign_root_id, Thread.base_dungeon, Thread.status).filter(
                Thread.campaign_root_id.in_(root_ids),
                Thread.campaign_role.in_(('faction_front', 'dungeon_front')),
                or_(Thread.status.in_(('seeded', 'active', 'escalated', 'closing')),
                    Thread.status == 'dormant' if investigation_maps else False),
                or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None)),
            ).distinct().all():
                if status != 'dormant' or name in (investigation_maps or {}).get(root_id, ()):
                    result.setdefault(root_id, set()).add(name)
        return result

    @staticmethod
    def completed_candidates(candidate_ids):
        if not candidate_ids:
            return []
        return db.session.query(Candidate, Log).join(Log, Log.id == Candidate.expedition_log_id).filter(
            Candidate.id.in_(candidate_ids), Log.status == 'completed').all()

    @staticmethod
    def lock_dispatches(dispatch_ids):
        if not dispatch_ids:
            return {}
        return {row.id: row for row in Dispatch.query.populate_existing().filter(
            Dispatch.id.in_(dispatch_ids)).order_by(Dispatch.id).with_for_update().all()}

    @staticmethod
    def admin_states(root_ids):
        if not root_ids:
            return {}
        rows = db.session.query(Dispatch, Candidate, Stage.status, Stage.plan['advance_mode'].as_string()).outerjoin(
            Candidate, Candidate.expedition_log_id == Dispatch.active_log_id,
        ).outerjoin(Stage, Stage.dispatch_id == Dispatch.id).filter(Dispatch.campaign_root_id.in_(root_ids)).all()
        occupancy = S1CampaignDispatchRepository.occupancy([row.id for row, *_ in rows])
        return {row.campaign_root_id: {
            'id': row.id, 'revision': row.revision,
            'discovered': row.discovered_at is not None, 'ended': row.ended_at is not None,
            'active_log_id': row.active_log_id, 'objective': row.objective,
            'max_active_teams': row.max_active_teams,
            'active_team_count': len(occupancy[row.id]) or int(row.active_log_id is not None),
            'busy_dungeons': occupancy[row.id],
            'maintenance_status': candidate.status if candidate else None,
            'maintenance_error': candidate.outline_error if candidate else None,
            'stage_status': stage_status,
            'stage_advance_mode': stage_mode or 'manual',
        } for row, candidate, stage_status, stage_mode in rows}

    @staticmethod
    def published_log_ids(log_ids):
        return {value for (value,) in db.session.query(Report.expedition_log_id).filter(
            Report.expedition_log_id.in_(log_ids)).all()} if log_ids else set()

    @staticmethod
    def release_failed_log(log_id):
        # Called with the existing narrative-lease release. Completion alone does
        # not release campaign ownership; explicit technical failure does.
        if not db.session.query(Log.id).filter_by(id=log_id, status='failed').first():
            return 0
        rows = Dispatch.query.populate_existing().filter(or_(Dispatch.active_log_id == log_id,
            db.session.query(Claim.expedition_log_id).filter(
                Claim.dispatch_id == Dispatch.id, Claim.expedition_log_id == log_id).exists()
        )).order_by(Dispatch.id).with_for_update().all()
        if not db.session.query(Log.id).filter_by(id=log_id, status='failed').first():
            return 0
        return S1CampaignDispatchRepository.release_owners(rows, {log_id}, now=_portable_utc_now())

    @staticmethod
    def root_is_occupied(root_id):
        row = Dispatch.query.populate_existing().filter_by(
            campaign_root_id=root_id).with_for_update().first()
        return row is not None and row.active_log_id is not None

    @staticmethod
    def log_is_occupied(log_id):
        return db.session.query(Log.id).filter(Log.id == log_id,
            S1CampaignDispatchRepository.ownership_exists(Log.id)).first() is not None

    @staticmethod
    def retry_publication(root_id):
        snapshot = Dispatch.query.filter_by(campaign_root_id=root_id).first()
        if not snapshot or not snapshot.active_log_id:
            return '没有等待发布的行动'
        candidate = Candidate.query.populate_existing().filter_by(
            expedition_log_id=snapshot.active_log_id).with_for_update().first()
        if not candidate or candidate.status != 'failed':
            return '只有世界维护失败的行动可以重试'
        log = Log.query.filter_by(id=candidate.expedition_log_id, status='completed').first()
        context = (log.full_log or {}).get('campaign_dispatch') if log else None
        if not context:
            return '行动的冻结来源不可用'
        thread = Thread.query.populate_existing().filter_by(id=context['thread_id']).with_for_update().first()
        row = S1CampaignDispatchRepository.get(snapshot.id, lock=True)
        if (not row or row.active_log_id != candidate.expedition_log_id
                or row.revision != context['revision']):
            return '行动已变化，请刷新'
        if not thread or int(thread.revision or 1) != context['source_revision']:
            return '前线版本已变化，需先检查原始世界维护回执'
        candidate.status = 'pending'
        candidate.maintenance_recovery_kind = None
        candidate.processed_at = None
        return None

    @staticmethod
    def release_failed_publications(*, limit, now, candidate_ids=None):
        """Release terminal publication or departure failures without crediting reports.

        The caller commits with the failure receipt, or reconciles older failures
        in a bounded maintenance transaction. Candidate -> dispatch lock order
        matches publication/retry; an old log can never clear a new log's claim.
        """
        if limit <= 0 or candidate_ids == []:
            return 0
        # Make a just-recorded terminal status visible to the guarded query.
        db.session.flush()
        # Generic lease cleanup must not acquire campaign leases before dispatch
        # rows. Reconcile abandoned failed departures here using the same ordered
        # dispatch -> lease release as publication, including nonrepresentative owners.
        failed_log_ids = set()
        if candidate_ids is None:
            failed_log_ids = {value for (value,) in db.session.query(Log.id).filter(
                Log.status == 'failed', S1CampaignDispatchRepository.ownership_exists(Log.id),
            ).order_by(Log.id).limit(limit).all()}
        query = Candidate.query.populate_existing().filter(
            Candidate.status == 'failed',
            ~db.session.query(Report.expedition_log_id).filter(
                Report.expedition_log_id == Candidate.expedition_log_id).exists(),
            db.session.query(Log.id).filter(Log.id == Candidate.expedition_log_id,
                Log.status == 'completed', S1CampaignDispatchRepository.ownership_exists(Log.id)).exists(),
        )
        if candidate_ids is not None:
            query = query.filter(Candidate.id.in_(candidate_ids))
        candidates = query.order_by(Candidate.id).with_for_update(skip_locked=True).limit(limit).all()
        if not candidates and not failed_log_ids:
            return 0
        publication_ids = {candidate.expedition_log_id for candidate in candidates}
        log_ids = publication_ids | failed_log_ids
        dispatches = Dispatch.query.populate_existing().filter(
            or_(Dispatch.active_log_id.in_(log_ids), db.session.query(Claim.expedition_log_id).filter(
                Claim.dispatch_id == Dispatch.id, Claim.expedition_log_id.in_(log_ids)).exists()),
        ).order_by(Dispatch.id).with_for_update().all()
        failed_log_ids = {value for (value,) in db.session.query(Log.id).filter(
            Log.id.in_(failed_log_ids), Log.status == 'failed').all()} if failed_log_ids else set()
        published = S1CampaignDispatchRepository.published_log_ids(publication_ids)
        return S1CampaignDispatchRepository.release_owners(
            dispatches, (publication_ids - published) | failed_log_ids, now=now)

    @staticmethod
    def publish(row, log, report, current_world_state, *, staged=False, revelation=False, report_revision=None):
        now = _portable_utc_now()
        stored_report = dict(report)
        if revelation or row.max_active_teams > 1:
            # Server-owned continuity, never an accepted AI report field or a
            # player-visible history field. Saved atomically with publication.
            stored_report['applied_world_state'] = current_world_state
        if row.max_active_teams > 1:
            # Publication order is independent of the task version frozen for
            # all concurrent squads. This proof is never supplied by the AI.
            stored_report['dispatch_revision'] = row.revision
        db.session.add(Report(expedition_log_id=log.id, dispatch_id=row.id,
                              revision=report_revision or row.revision,
                              party_name=log.party_name or '', report=stored_report))
        row.public_title = report['public_title']
        row.public_summary = report['public_summary']
        row.current_world_state = current_world_state
        if row.max_active_teams <= 1:
            row.objective = report['next_objective']
            row.target_dungeons = report['target_dungeons']
        row.discovered_at = row.discovered_at or now
        row.ended_at = None if staged or row.objective else now
        if row.max_active_teams <= 1:
            row.revision += 1
        S1CampaignDispatchRepository.release_owners([row], {log.id}, now=now)
        row.updated_at = now
        return True
