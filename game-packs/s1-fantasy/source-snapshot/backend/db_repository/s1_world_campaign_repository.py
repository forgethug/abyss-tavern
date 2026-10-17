"""Repository queries for operator-authored S1 world campaigns.

Campaigns deliberately reuse ``AdventurerS1WorldStoryThread`` and its existing
faction links.  This repository only owns query shape and lock order; lifecycle
semantics remain in the S1 campaign/world-outline services.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, text

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1FactionProfile,
    AdventurerS1FactionProfileThread,
    AdventurerS1WorldStoryThread,
)


_ACTIVE_THREAD_STATUSES = (
    'seeded', 'active', 'escalated', 'closing', 'dormant',
)
_CAMPAIGN_FRONT_ROLES = ('faction_front', 'dungeon_front')
_CAMPAIGN_WRITE_LOCK_KEY = 'gupiao:s1_world_campaign_create:v1'


class S1WorldCampaignRepository:
    """Bounded campaign reads plus deterministic transactional locks."""

    @staticmethod
    def acquire_campaign_write_lock() -> None:
        """Serialize campaign creation even when no campaign row exists yet."""
        bind = db.session.get_bind()
        if bind is not None and bind.dialect.name == 'postgresql':
            db.session.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _CAMPAIGN_WRITE_LOCK_KEY},
            )

    @staticmethod
    def get_root_by_key(
        campaign_key: str,
        *,
        for_update: bool = False,
    ) -> AdventurerS1WorldStoryThread | None:
        query = db.session.query(AdventurerS1WorldStoryThread).filter(
            AdventurerS1WorldStoryThread.campaign_key == campaign_key,
            AdventurerS1WorldStoryThread.campaign_role == 'spine',
        )
        if for_update:
            query = query.with_for_update()
        return query.first()

    @staticmethod
    def get_campaign_thread_for_update(
        thread_id: int,
    ) -> AdventurerS1WorldStoryThread | None:
        return (
            db.session.query(AdventurerS1WorldStoryThread)
            .filter(
                AdventurerS1WorldStoryThread.id == int(thread_id),
                AdventurerS1WorldStoryThread.campaign_root_id.isnot(None),
                AdventurerS1WorldStoryThread.campaign_role.in_((
                    'spine', *_CAMPAIGN_FRONT_ROLES,
                )),
                or_(
                    AdventurerS1WorldStoryThread.campaign_role == 'spine',
                    AdventurerS1WorldStoryThread.is_chain_head.is_(True),
                    AdventurerS1WorldStoryThread.is_chain_head.is_(None),
                ),
            )
            .with_for_update()
            .first()
        )

    @staticmethod
    def lock_faction_profiles(
        profile_ids: Iterable[int],
    ) -> list[AdventurerS1FactionProfile]:
        ids = sorted({int(value) for value in profile_ids})
        if not ids:
            return []
        return (
            db.session.query(AdventurerS1FactionProfile)
            .filter(AdventurerS1FactionProfile.id.in_(ids))
            .order_by(AdventurerS1FactionProfile.id.asc())
            .with_for_update()
            .all()
        )

    @staticmethod
    def get_faction_profiles(
        profile_ids: Iterable[int],
    ) -> list[AdventurerS1FactionProfile]:
        ids = sorted({int(value) for value in profile_ids})
        if not ids:
            return []
        return (
            db.session.query(AdventurerS1FactionProfile)
            .filter(AdventurerS1FactionProfile.id.in_(ids))
            .order_by(AdventurerS1FactionProfile.id.asc())
            .all()
        )

    @staticmethod
    def find_title_conflicts(
        titles: Iterable[str],
        *,
        for_update: bool = False,
    ) -> list[AdventurerS1WorldStoryThread]:
        values = sorted({str(value).strip() for value in titles if str(value).strip()})
        if not values:
            return []
        query = (
            db.session.query(AdventurerS1WorldStoryThread)
            .filter(AdventurerS1WorldStoryThread.title.in_(values))
            .order_by(AdventurerS1WorldStoryThread.id.asc())
        )
        if for_update:
            query = query.with_for_update()
        return query.all()

    @staticmethod
    def add_world_story_threads(
        rows: Iterable[AdventurerS1WorldStoryThread],
    ) -> None:
        db.session.add_all(list(rows))

    @staticmethod
    def add_faction_links(
        rows: Iterable[AdventurerS1FactionProfileThread],
    ) -> None:
        db.session.add_all(list(rows))

    @staticmethod
    def flush() -> None:
        db.session.flush()

    @staticmethod
    def list_faction_profiles(
        *,
        page: int,
        per_page: int,
        status: str | None,
        keyword: str | None,
    ) -> tuple[list[AdventurerS1FactionProfile], int]:
        query = db.session.query(AdventurerS1FactionProfile)
        if status:
            query = query.filter(AdventurerS1FactionProfile.status == status)
        if keyword:
            like = f'%{keyword}%'
            query = query.filter(or_(
                AdventurerS1FactionProfile.faction_key.ilike(like),
                AdventurerS1FactionProfile.canonical_name.ilike(like),
                AdventurerS1FactionProfile.identity_summary.ilike(like),
            ))
        total = int(query.count())
        rows = (
            query.order_by(
                AdventurerS1FactionProfile.importance.desc(),
                AdventurerS1FactionProfile.updated_at.desc(),
                AdventurerS1FactionProfile.id.asc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return rows, total

    @staticmethod
    def list_campaign_roots(
        *,
        page: int,
        per_page: int,
        status: str | None,
        keyword: str | None,
    ) -> tuple[list[AdventurerS1WorldStoryThread], int]:
        query = db.session.query(AdventurerS1WorldStoryThread).filter(
            AdventurerS1WorldStoryThread.campaign_role == 'spine',
            AdventurerS1WorldStoryThread.campaign_key.isnot(None),
        )
        if status:
            query = query.filter(AdventurerS1WorldStoryThread.status == status)
        if keyword:
            like = f'%{keyword}%'
            query = query.filter(or_(
                AdventurerS1WorldStoryThread.campaign_key.ilike(like),
                AdventurerS1WorldStoryThread.title.ilike(like),
                AdventurerS1WorldStoryThread.summary.ilike(like),
                AdventurerS1WorldStoryThread.campaign_agenda.ilike(like),
            ))
        total = int(query.count())
        rows = (
            query.order_by(
                AdventurerS1WorldStoryThread.updated_at.desc(),
                AdventurerS1WorldStoryThread.id.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return rows, total

    @staticmethod
    def load_campaign_graph(
        root_ids: Iterable[int],
    ) -> tuple[
        dict[int, list[AdventurerS1WorldStoryThread]],
        dict[int, list[AdventurerS1FactionProfileThread]],
        dict[int, AdventurerS1FactionProfile],
    ]:
        roots = sorted({int(value) for value in root_ids})
        if not roots:
            return {}, {}, {}
        fronts = (
            db.session.query(AdventurerS1WorldStoryThread)
            .filter(
                AdventurerS1WorldStoryThread.campaign_root_id.in_(roots),
                AdventurerS1WorldStoryThread.campaign_role.in_(
                    _CAMPAIGN_FRONT_ROLES
                ),
                or_(
                    AdventurerS1WorldStoryThread.is_chain_head.is_(True),
                    AdventurerS1WorldStoryThread.is_chain_head.is_(None),
                ),
            )
            .order_by(
                AdventurerS1WorldStoryThread.campaign_root_id.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .all()
        )
        fronts_by_root: dict[int, list[AdventurerS1WorldStoryThread]] = {}
        for row in fronts:
            fronts_by_root.setdefault(int(row.campaign_root_id), []).append(row)

        front_ids = [int(row.id) for row in fronts]
        if not front_ids:
            return fronts_by_root, {}, {}
        links = (
            db.session.query(AdventurerS1FactionProfileThread)
            .filter(
                AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                    front_ids
                )
            )
            .order_by(
                AdventurerS1FactionProfileThread.world_story_thread_id.asc(),
                AdventurerS1FactionProfileThread.faction_profile_id.asc(),
            )
            .all()
        )
        links_by_thread: dict[int, list[AdventurerS1FactionProfileThread]] = {}
        for row in links:
            links_by_thread.setdefault(int(row.world_story_thread_id), []).append(row)

        profile_ids = sorted({int(row.faction_profile_id) for row in links})
        profiles = (
            db.session.query(AdventurerS1FactionProfile)
            .filter(AdventurerS1FactionProfile.id.in_(profile_ids))
            .all()
        ) if profile_ids else []
        profiles_by_id = {int(row.id): row for row in profiles}
        return fronts_by_root, links_by_thread, profiles_by_id

    @staticmethod
    def list_active_threads_for_dungeons(
        base_dungeons: Iterable[str],
    ) -> list[AdventurerS1WorldStoryThread]:
        dungeons = sorted({
            str(value).strip() for value in base_dungeons if str(value).strip()
        })
        if not dungeons:
            return []
        return (
            db.session.query(AdventurerS1WorldStoryThread)
            .filter(
                AdventurerS1WorldStoryThread.base_dungeon.in_(dungeons),
                AdventurerS1WorldStoryThread.status.in_(_ACTIVE_THREAD_STATUSES),
                or_(
                    AdventurerS1WorldStoryThread.is_chain_head.is_(True),
                    AdventurerS1WorldStoryThread.is_chain_head.is_(None),
                ),
                or_(
                    AdventurerS1WorldStoryThread.campaign_role.is_(None),
                    AdventurerS1WorldStoryThread.campaign_role != 'spine',
                ),
            )
            .order_by(
                AdventurerS1WorldStoryThread.base_dungeon.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .all()
        )

    @staticmethod
    def list_active_campaign_links_for_profiles(
        profile_ids: Iterable[int],
    ) -> list[tuple[AdventurerS1FactionProfileThread, AdventurerS1WorldStoryThread]]:
        ids = sorted({int(value) for value in profile_ids})
        if not ids:
            return []
        return (
            db.session.query(
                AdventurerS1FactionProfileThread,
                AdventurerS1WorldStoryThread,
            )
            .join(
                AdventurerS1WorldStoryThread,
                AdventurerS1WorldStoryThread.id
                == AdventurerS1FactionProfileThread.world_story_thread_id,
            )
            .filter(
                AdventurerS1FactionProfileThread.faction_profile_id.in_(ids),
                AdventurerS1WorldStoryThread.campaign_root_id.isnot(None),
                AdventurerS1WorldStoryThread.campaign_role.in_(
                    _CAMPAIGN_FRONT_ROLES
                ),
                AdventurerS1WorldStoryThread.status.in_(_ACTIVE_THREAD_STATUSES),
                or_(
                    AdventurerS1WorldStoryThread.is_chain_head.is_(True),
                    AdventurerS1WorldStoryThread.is_chain_head.is_(None),
                ),
            )
            .order_by(
                AdventurerS1FactionProfileThread.faction_profile_id.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .all()
        )
