"""S1 公共剧情素材注入租约。

租约只锁定结构化注入素材（接力便签 / 公共世界线），不做玩家可见正文关键字过滤。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import exists, func, or_

from db_repository import db_manager
from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository as Campaigns
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog,
    AdventurerS1NarrativeLease,
    AdventurerS1WorldActor,
    AdventurerS1WorldStoryThread,
    AdventurerS1WorldStoryThreadActor,
)


def _lease_enabled() -> bool:
    return bool(current_app.config.get('ADVENTURER_S1_NARRATIVE_LEASE_ENABLED', True))


def _lease_ttl_hours() -> int:
    try:
        return max(1, int(current_app.config.get('ADVENTURER_S1_NARRATIVE_LEASE_TTL_HOURS', 8)))
    except (TypeError, ValueError):
        return 8


def _clip_signature(value: str) -> str:
    return str(value or '').strip()[:180]


def _owner_still_active():
    """Campaign owners release centrally in dispatch -> lease order, including failures."""
    return or_(AdventurerS1ExpeditionLog.status.in_(('generating', 'in_progress')),
               Campaigns.ownership_exists(AdventurerS1ExpeditionLog.id))


def _expire_stale_leases(now: datetime | None = None) -> int:
    """Atomically expire every lease that is still active at the cutoff."""
    now = now or _portable_utc_now()
    active_owner_exists = exists().where(
        AdventurerS1ExpeditionLog.id == AdventurerS1NarrativeLease.expedition_log_id,
        _owner_still_active(),
    )
    return AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.status == 'active',
        AdventurerS1NarrativeLease.expires_at <= now,
        ~active_owner_exists,
    ).update(
        {
            AdventurerS1NarrativeLease.status: 'expired',
            AdventurerS1NarrativeLease.released_at: func.coalesce(
                AdventurerS1NarrativeLease.released_at,
                now,
            ),
            AdventurerS1NarrativeLease.updated_at: now,
        },
        synchronize_session=False,
    )


def get_world_actor_ids_leased_by_other_expeditions(
    expedition_log_id: int | None,
) -> set[int]:
    """Return actors currently reserved by another unfinished expedition.

    This is a read-only projection for prompt assembly. The owning log state is
    authoritative, so a long-running active expedition keeps the actor even when
    its safety TTL has elapsed; recovery releases abandoned owners separately.
    """
    if not _lease_enabled():
        return set()
    query = AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.lease_type == 'world_actor',
        AdventurerS1NarrativeLease.status == 'active',
        AdventurerS1NarrativeLease.world_actor_id.isnot(None),
    ).join(
        AdventurerS1ExpeditionLog,
        AdventurerS1ExpeditionLog.id == AdventurerS1NarrativeLease.expedition_log_id,
    ).filter(
        _owner_still_active(),
    )
    if expedition_log_id:
        query = query.filter(
            AdventurerS1NarrativeLease.expedition_log_id != int(expedition_log_id),
        )
    return {
        int(actor_id)
        for actor_id, in query.with_entities(
            AdventurerS1NarrativeLease.world_actor_id,
        ).distinct().all()
        if actor_id is not None
    }


def get_world_actor_ids_leased_by_expedition(
    expedition_log_id: int | None,
) -> set[int]:
    """Return active world-actor reservations owned by one expedition log."""
    if not _lease_enabled() or not expedition_log_id:
        return set()
    return {
        int(actor_id)
        for actor_id, in AdventurerS1NarrativeLease.query.filter(
            AdventurerS1NarrativeLease.lease_type == 'world_actor',
            AdventurerS1NarrativeLease.status == 'active',
            AdventurerS1NarrativeLease.expedition_log_id == int(expedition_log_id),
            AdventurerS1NarrativeLease.world_actor_id.isnot(None),
        ).with_entities(
            AdventurerS1NarrativeLease.world_actor_id,
        ).distinct().all()
        if actor_id is not None
    }


def release_inactive_expedition_narrative_leases(
    *,
    now: datetime | None = None,
) -> int:
    """Release expired, terminal-log, and orphaned expedition leases in bulk."""
    now = now or _portable_utc_now()
    released = _expire_stale_leases(now)
    terminal_owner_exists = exists().where(
        AdventurerS1ExpeditionLog.id
        == AdventurerS1NarrativeLease.expedition_log_id,
        ~_owner_still_active(),
    )
    released += AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.status == 'active',
        terminal_owner_exists,
    ).update(
        {
            AdventurerS1NarrativeLease.status: 'released',
            AdventurerS1NarrativeLease.released_at: func.coalesce(
                AdventurerS1NarrativeLease.released_at,
                now,
            ),
            AdventurerS1NarrativeLease.updated_at: now,
        },
        synchronize_session=False,
    )
    # A fresh lease whose owner row is not yet visible may belong to a
    # concurrently committing placeholder. Keep it until the TTL; expired
    # orphans were already handled by _expire_stale_leases above.
    return int(released)


def acquire_narrative_lease(
    *,
    target_dungeon: str,
    plot_signature: str,
    lease_type: str,
    party_id: int | None = None,
    expedition_log_id: int | None = None,
    world_story_thread_id: int | None = None,
    world_actor_id: int | None = None,
    dm_handoff_state_id: int | None = None,
    source_log_id: int | None = None,
    title: str | None = None,
    required_adventurer_ids: list[int] | None = None,
    required_item_names: list[str] | None = None,
) -> bool:
    """尝试占用一条剧情素材；已有其他未释放探险占用时返回 False。

    调用方应处于事务内。若未提供 expedition_log_id，仅做兼容放行，不创建租约。
    """
    dungeon = str(target_dungeon or '').strip()
    signature = _clip_signature(plot_signature)
    if not _lease_enabled() or not dungeon or not signature or not expedition_log_id:
        return True

    now = _portable_utc_now()
    release_inactive_expedition_narrative_leases(now=now)
    active_rows = (
        AdventurerS1NarrativeLease.query
        .filter_by(target_dungeon=dungeon, plot_signature=signature, status='active')
        .with_for_update()
        .all()
    )
    for row in active_rows:
        if int(row.expedition_log_id or 0) == int(expedition_log_id):
            row.expires_at = max(row.expires_at, now + timedelta(hours=_lease_ttl_hours()))
            row.updated_at = now
            return True
        return False

    lease = AdventurerS1NarrativeLease(
        target_dungeon=dungeon,
        plot_signature=signature,
        lease_type=str(lease_type or '')[:30] or 'unknown',
        status='active',
        party_id=party_id,
        expedition_log_id=expedition_log_id,
        world_story_thread_id=world_story_thread_id,
        world_actor_id=world_actor_id,
        dm_handoff_state_id=dm_handoff_state_id,
        source_log_id=source_log_id,
        required_adventurer_ids=[int(x) for x in (required_adventurer_ids or []) if str(x).isdigit()] or None,
        required_item_names=[str(x).strip() for x in (required_item_names or []) if str(x).strip()] or None,
        title=str(title or '').strip()[:160] or None,
        expires_at=now + timedelta(hours=_lease_ttl_hours()),
    )
    db_manager.add(lease, flush=False)
    return True


def acquire_world_story_bundle_lease(
    *,
    target_dungeon: str,
    world_story_thread_id: int,
    party_id: int | None,
    expedition_log_id: int | None,
    source_log_id: int | None = None,
    title: str | None = None,
    campaign_dispatch_id: int | None = None,
) -> bool:
    """原子占用世界线及其直接角色；角色在任意地牢都只能被一个未结算探险直接占用。"""
    if not expedition_log_id:
        return True
    lease_enabled = _lease_enabled()
    now = _portable_utc_now()
    if lease_enabled:
        release_inactive_expedition_narrative_leases(now=now)
    thread = AdventurerS1WorldStoryThread.query.filter_by(id=int(world_story_thread_id)).with_for_update().first()
    if not thread:
        return False
    from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
    if not S1CampaignDispatchRepository.front_access_allowed(
        thread.campaign_root_id, expedition_log_id, campaign_dispatch_id,
    ):
        return False
    if not lease_enabled:
        return True
    links = AdventurerS1WorldStoryThreadActor.query.filter_by(
        world_story_thread_id=int(world_story_thread_id),
        lease_required=True,
    ).all()
    actor_ids = sorted({int(link.actor_id) for link in links})
    if actor_ids:
        # 锁角色本体而非不存在的租约行，保证两个首次并发申请也会串行复查。
        locked_ids = [int(row.id) for row in AdventurerS1WorldActor.query.filter(
            AdventurerS1WorldActor.id.in_(actor_ids),
        ).order_by(AdventurerS1WorldActor.id.asc()).with_for_update().all()]
        if locked_ids != actor_ids:
            return False
        occupied = AdventurerS1NarrativeLease.query.filter(
            AdventurerS1NarrativeLease.world_actor_id.in_(actor_ids),
            AdventurerS1NarrativeLease.status == 'active',
            AdventurerS1NarrativeLease.expedition_log_id != int(expedition_log_id),
        ).first()
        if occupied:
            return False
    signature = f'world_story_thread:{int(world_story_thread_id)}'
    occupied_thread = AdventurerS1NarrativeLease.query.filter_by(
        plot_signature=signature,
        status='active',
    ).filter(AdventurerS1NarrativeLease.expedition_log_id != int(expedition_log_id)).first()
    if occupied_thread:
        return False
    expires_at = now + timedelta(hours=_lease_ttl_hours())
    existing_thread = AdventurerS1NarrativeLease.query.filter_by(
        plot_signature=signature,
        status='active',
        expedition_log_id=int(expedition_log_id),
    ).first()
    if existing_thread:
        existing_thread.expires_at = max(existing_thread.expires_at, expires_at)
    else:
        db_manager.add(AdventurerS1NarrativeLease(
            target_dungeon=str(target_dungeon or '').strip(),
            plot_signature=signature,
            lease_type='world_story_thread',
            status='active',
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            world_story_thread_id=world_story_thread_id,
            source_log_id=source_log_id,
            title=str(title or '').strip()[:160] or None,
            expires_at=expires_at,
        ), flush=False)
    for actor_id in actor_ids:
        existing_actor = AdventurerS1NarrativeLease.query.filter_by(
            world_actor_id=actor_id,
            status='active',
            expedition_log_id=int(expedition_log_id),
        ).first()
        if existing_actor:
            existing_actor.expires_at = max(existing_actor.expires_at, expires_at)
            continue
        db_manager.add(AdventurerS1NarrativeLease(
            target_dungeon=str(target_dungeon or '').strip(),
            plot_signature=f'world_actor:{actor_id}',
            lease_type='world_actor',
            status='active',
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            world_story_thread_id=world_story_thread_id,
            world_actor_id=actor_id,
            source_log_id=source_log_id,
            title=str(title or '').strip()[:160] or None,
            expires_at=expires_at,
        ), flush=False)
    return True


def release_narrative_leases_for_log(expedition_log_id: int | None, *, status: str = 'released') -> int:
    """释放指定探险日志占用的剧情租约；调用方可在事务内或事务外使用。"""
    if not expedition_log_id:
        return 0
    Campaigns.release_failed_log(int(expedition_log_id))
    if Campaigns.log_is_occupied(int(expedition_log_id)):
        return 0
    now = _portable_utc_now()
    rows = AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.expedition_log_id == int(expedition_log_id),
        AdventurerS1NarrativeLease.status == 'active',
    ).all()
    next_status = 'expired' if status == 'expired' else 'released'
    for row in rows:
        row.status = next_status
        row.released_at = now
        row.updated_at = now
    return len(rows)


def release_unadopted_world_story_leases(
    expedition_log_id: int | None,
    *,
    adopted_world_story_thread_ids: set[int] | None = None,
) -> int:
    """Release unadopted world-story leases while retaining injected actors.

    A non-terminal actor is reserved from prompt injection until the expedition
    reaches a terminal path. Releasing an ignored/background thread must not
    make that actor visible to another concurrent expedition.
    """
    if not expedition_log_id:
        return 0
    adopted_ids = {
        int(value) for value in (adopted_world_story_thread_ids or set())
        if int(value) > 0
    }
    rows = AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.expedition_log_id == int(expedition_log_id),
        AdventurerS1NarrativeLease.status == 'active',
        AdventurerS1NarrativeLease.lease_type == 'world_story_thread',
    ).all()
    now = _portable_utc_now()
    released = 0
    for row in rows:
        if int(row.world_story_thread_id or 0) in adopted_ids:
            continue
        row.status = 'released'
        row.released_at = now
        row.updated_at = now
        released += 1
    return released


def release_narrative_leases_for_party(party_id: int | None, *, status: str = 'released') -> int:
    """释放指定小队当前占用的剧情租约。"""
    if not party_id:
        return 0
    now = _portable_utc_now()
    rows = AdventurerS1NarrativeLease.query.filter(
        AdventurerS1NarrativeLease.party_id == int(party_id),
        AdventurerS1NarrativeLease.status == 'active',
    ).all()
    next_status = 'expired' if status == 'expired' else 'released'
    for row in rows:
        row.status = next_status
        row.released_at = now
        row.updated_at = now
    return len(rows)
