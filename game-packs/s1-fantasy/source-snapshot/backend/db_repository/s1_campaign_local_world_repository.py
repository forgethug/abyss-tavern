"""Bounded, realm-local world evidence for a campaign's phase handoff."""
from copy import deepcopy
from sqlalchemy import and_, or_

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1WorldLocationState as Location,
    AdventurerS1WorldActor as Actor,
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1WorldStoryThreadActor as ActorLink,
    AdventurerS1WorldLocationStateHistory as LocationHistory,
    AdventurerS1CampaignDispatchReport as Report,
    AdventurerS1ExpeditionLog as Log,
)


def _rows(query, maximum, label, *, lock):
    if maximum < 1:
        raise ValueError('战役当地事实读取上限必须为正数')
    if lock:
        # Final handoff only: don't wait on another world's write lock while holding a campaign.
        query = query.with_for_update(read=True, nowait=True)
    rows = query.limit(maximum + 1).all()
    if len(rows) > maximum:
        raise ValueError(f'战役当地{label}超过完整快照上限，请核对地图范围或配置')
    return rows


def campaign_location_scopes(root_id, *, batch_size):
    # Retired predecessors remain part of this campaign's exact location history.
    return db.session.query(Thread.base_dungeon, Thread.scope_keys).filter(
        Thread.campaign_root_id == root_id, Thread.campaign_role == 'dungeon_front',
    ).order_by(Thread.id).yield_per(batch_size)


def published_campaign_loci(dispatch_id, *, batch_size):
    # Stream only the small identity document, never historical story bodies.
    return db.session.query(Log.id, Log.target_dungeon,
        Log.full_log['_expedition_locus_snapshot'].label('locus'),
    ).join(Report, Report.expedition_log_id == Log.id).filter(
        Report.dispatch_id == dispatch_id, Report.published_at.isnot(None),
        Log.status == 'completed',
    ).order_by(Log.id).yield_per(batch_size)


def published_campaign_changed_locations(dispatch_id, *, maximum):
    # The successful writeback history preserves all independent locations, including
    # multiple materialized locations in one new-frontier expedition.
    return _rows(db.session.query(LocationHistory.location_state_id, Log.target_dungeon).join(
        Log, Log.id == LocationHistory.source_log_id,
    ).join(Report, Report.expedition_log_id == Log.id).filter(
        Report.dispatch_id == dispatch_id, Report.published_at.isnot(None),
        Log.status == 'completed',
    ).distinct().order_by(LocationHistory.location_state_id, Log.target_dungeon),
        maximum, '关联地点', lock=False)


def load_campaign_local_world(dungeons, *, location_refs, max_locations, actor_batch_size, lock=False):
    maps = sorted(set(dungeons))
    if not maps:
        return {'dungeons': [], 'locations': [], 'actors': []}
    if actor_batch_size < 1:
        raise ValueError('战役当地人物读取批量必须为正数')
    # Only exact campaign locations and the map's own aggregate state belong to
    # this handoff. Unrelated rooms on a large map must not exhaust its budget
    # or invalidate an approved proposal when another party visits them.
    locations_by_id = {}
    for dungeon, location_id in location_refs:
        if dungeon not in maps or type(location_id) is not int or location_id < 1:
            raise ValueError('战役地点引用身份无效')
        if location_id in locations_by_id and locations_by_id[location_id] != dungeon:
            raise ValueError('战役地点引用跨地图冲突')
        locations_by_id[location_id] = dungeon
    if len(locations_by_id) > max_locations:
        raise ValueError('战役关联地点超过完整快照上限')
    if locations_by_id:
        identity_query = db.session.query(Location.id, Location.target_dungeon).filter(
            Location.id.in_(locations_by_id))
        if lock:
            identity_query = identity_query.with_for_update(read=True, nowait=True)
        identities = identity_query.all()
        if dict(identities) != locations_by_id:
            raise ValueError('战役关联地点缺失或地图身份已经改变')
    locations = _rows(db.session.query(
        Location.id, Location.target_dungeon, Location.canonical_name,
        Location.current_state, Location.revision, Location.last_changed_log_id,
    ).filter(Location.target_dungeon.in_(maps), Location.current_state != '', or_(
        Location.id.in_(locations_by_id),
        and_(Location.canonical_name == Location.target_dungeon,
             Location.target_dungeon.in_(maps)),
    )).order_by(Location.id),
        max_locations, '地点', lock=lock)
    if any(row.id in locations_by_id and row.target_dungeon != locations_by_id[row.id]
           for row in locations):
        raise ValueError('战役关联地点地图身份已经改变')
    # Exact persisted links include resolved histories and actors not linked to this campaign.
    # A historic dungeon association does not assert the actor is still present there.
    local_actor_ids = db.session.query(ActorLink.actor_id).join(
        Thread, Thread.id == ActorLink.world_story_thread_id,
    ).filter(Thread.base_dungeon.in_(maps))
    fields = ('id', 'canonical_name', 'status', 'identity_summary', 'power_boundary',
              'capability_profile', 'capability_revision', 'current_state', 'current_agenda',
              'scope_keys')
    actor_query = db.session.query(*(getattr(Actor, key) for key in fields)).filter(
        Actor.id.in_(local_actor_ids)).order_by(Actor.id)
    if lock:
        actor_query = actor_query.with_for_update(read=True, nowait=True)
    # Map history grows independently of a campaign. Stream the complete exact
    # association, without a row-count cutoff or duplicate rows from its links.
    # The final purpose-specific wire view still uses the shared AI input budget.
    actors = actor_query.yield_per(actor_batch_size)
    def project(row):
        return {key: deepcopy(value) for key, value in row._mapping.items()}
    return {'dungeons': maps, 'locations': [project(row) for row in locations],
            'actors': [project(row) for row in actors]}
