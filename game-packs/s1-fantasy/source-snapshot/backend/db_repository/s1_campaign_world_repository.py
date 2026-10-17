"""Catalog and memory boundary for campaign-owned, fresh dungeon names."""
from sqlalchemy import select, union, or_
from sqlalchemy.orm import aliased

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1WorldStoryThread as Thread,
    AdventurerS1WorldLocationState as Location,
    AdventurerS1ExpeditionLog as Log,
    AdventurerS1DmHandoffState as Handoff,
    AdventurerS1WorldActor as Actor,
    AdventurerS1FactionProfile as Faction,
    AdventurerS1CampaignDispatch as Dispatch,
    AdventurerS1CampaignDispatchReport as Report,
)


class S1CampaignWorldRepository:
    @staticmethod
    def applied_local_state(root_id, dungeon):
        """Only real local progress enters ambient exploration, never future hints."""
        row = db.session.query(Thread.summary).filter(
            Thread.campaign_root_id == root_id, Thread.base_dungeon == dungeon,
            Thread.campaign_role.in_(('dungeon_front', 'faction_front')),
            or_(Thread.is_chain_head.is_(True), Thread.is_chain_head.is_(None)),
            Thread.last_progress_log_id.isnot(None),
        ).order_by(Thread.updated_at.desc(), Thread.id.desc()).first()
        if row:
            return row[0]
        # A new seeded successor has not acted yet. Preserve the applied report
        # or predecessor's real outcomes, without treating its future hint as fact.
        report = db.session.query(Report.report).join(Dispatch, Dispatch.id == Report.dispatch_id).join(
            Log, Log.id == Report.expedition_log_id).filter(
                Dispatch.campaign_root_id == root_id, Log.target_dungeon == dungeon,
                Report.published_at.isnot(None)).order_by(Report.revision.desc()).first()
        if report and report[0].get('applied_world_state'):
            return report[0]['applied_world_state']
        predecessor = db.session.query(Thread.summary).filter(
            Thread.campaign_root_id == root_id, Thread.base_dungeon == dungeon,
            Thread.campaign_role.in_(('dungeon_front', 'faction_front')),
            Thread.last_progress_log_id.isnot(None),
        ).order_by(Thread.updated_at.desc(), Thread.id.desc()).first()
        return predecessor[0] if predecessor else None

    @staticmethod
    def worlds_for_roots(root_ids):
        """One bounded catalogue read for a page of campaign entry points."""
        if not root_ids:
            return {}
        rows = db.session.query(Thread.id, Thread.campaign_world).filter(
            Thread.id.in_(root_ids), Thread.campaign_role == 'spine',
            Thread.campaign_world.isnot(None)).all()
        return {
            entry['name']: {**entry, 'campaign_root_id': root_id,
                            'dungeons': [item['name'] for item in world['dungeons']]}
            for root_id, world in rows for entry in world['dungeons']
        }

    @staticmethod
    def worlds_for_dungeons(names):
        names = set(names)
        if not names:
            return {}
        root = aliased(Thread)
        root_ids = select(Thread.campaign_root_id).where(Thread.base_dungeon.in_(names))
        rows = db.session.query(root.id, root.campaign_world).filter(
            root.campaign_role == 'spine', root.campaign_world.isnot(None), root.id.in_(root_ids)).all()
        return {
            entry['name']: {**entry, 'campaign_root_id': root_id,
                            'dungeons': [item['name'] for item in world['dungeons']]}
            for root_id, world in rows for entry in world['dungeons']
            if entry['name'] in names
        }

    @staticmethod
    def occupied_names(names):
        """Including terminal history: ending an activity never frees its names."""
        names = set(names)
        if not names:
            return []
        query = union(
            select(Thread.base_dungeon).where(Thread.base_dungeon.in_(names)),
            select(Location.target_dungeon).where(Location.target_dungeon.in_(names)),
            select(Location.canonical_name).where(Location.canonical_name.in_(names)),
            select(Handoff.target_dungeon).where(Handoff.target_dungeon.in_(names)),
            select(Log.target_dungeon).where(Log.target_dungeon.in_(names)),
            select(Log.hidden_dungeon).where(Log.hidden_dungeon.in_(names)),
            select(Log.temporary_location_name).where(Log.temporary_location_name.in_(names)),
        )
        return sorted(db.session.execute(query).scalars().all())

    @staticmethod
    def assert_owned_updates(maintained, world):
        """Recheck existing identities at the transactional consumer boundary."""
        maps = set(world['dungeons'])
        for field, model in (('world_actors', Actor), ('faction_profile_updates', Faction)):
            ids = {row['id'] for row in maintained.get(field) or [] if type(row.get('id')) is int}
            if field == 'world_actors':
                ids.update(row['continuity_actor_id'] for row in maintained.get('world_story_threads') or []
                           if type(row.get('continuity_actor_id')) is int)
            if not ids:
                continue
            owned = {row_id for row_id, in db.session.query(model.id).join(
                Log, model.first_seen_log_id == Log.id,
            ).filter(model.id.in_(ids), Log.target_dungeon.in_(maps)).all()}
            if not ids.issubset(owned):
                raise ValueError('activity_world_identity_out_of_scope:' + field)
        from plugins.adventurer_s1.services.s1_campaign_world_service import activity_maintenance_errors
        scope_errors = activity_maintenance_errors(maintained, world,
            [{'id': row['id']} for row in maintained.get('world_actors') or [] if 'id' in row],
            [{'id': row['id']} for row in maintained.get('faction_profile_updates') or [] if 'id' in row],
            allowed_location_scopes=S1CampaignWorldRepository.owned_location_scopes(maps))
        if scope_errors:
            raise ValueError(scope_errors[0])
        faction_keys = {relation['faction_key'] for row in maintained.get('world_actors') or []
                        for relation in row.get('faction_relations') or [] if relation.get('faction_key')}
        if faction_keys:
            linked = db.session.query(Faction.faction_key, Log.target_dungeon).outerjoin(
                Log, Faction.first_seen_log_id == Log.id,
            ).filter(Faction.faction_key.in_(faction_keys)).all()
            if any(target not in maps for _, target in linked):
                raise ValueError('activity_world_faction_relation_out_of_scope')
        rows = maintained.get('world_story_threads') or []
        ids = {row[key] for row in rows for key in ('id', 'predecessor_id', 'source_world_story_thread_id')
               if type(row.get(key)) is int}
        links = [link for field in ('world_actors', 'faction_profile_updates')
                 for row in maintained.get(field) or [] for link in row.get('thread_links') or []]
        ids.update(link['thread_id'] for link in links if type(link.get('thread_id')) is int)
        titles = {link['thread_title'] for link in links if isinstance(link.get('thread_title'), str)}
        if ids or titles:
            threads = Thread.query.with_entities(Thread.id, Thread.title, Thread.base_dungeon).filter(
                or_(Thread.id.in_(ids), Thread.title.in_(titles))).all()
            if any(row.base_dungeon not in maps for row in threads):
                raise ValueError('activity_world_thread_out_of_scope')

    @staticmethod
    def owned_actor_ids(actor_ids, maps):
        return {row_id for row_id, in db.session.query(Actor.id).join(
            Log, Actor.first_seen_log_id == Log.id,
        ).filter(Actor.id.in_(actor_ids), Log.target_dungeon.in_(maps)).all()}

    @staticmethod
    def owned_location_scopes(maps):
        return {f's1_location_state:{row_id}' for row_id, in db.session.query(Location.id).filter(
            Location.target_dungeon.in_(maps)).all()}

    @staticmethod
    def isolated_dungeon_names():
        root = aliased(Thread)
        isolated_roots = select(root.id).where(root.campaign_world.isnot(None))
        return select(Thread.base_dungeon).where(
            Thread.campaign_root_id.in_(isolated_roots),
            Thread.campaign_role != 'spine', Thread.base_dungeon.isnot(None))

    @staticmethod
    def entity_scope(model, dungeon):
        """Origin identity is authoritative even when an entity has global scopes."""
        from plugins.adventurer_s1.services.s1_campaign_world_service import campaign_world
        world = campaign_world(dungeon)
        maps = world['dungeons'] if world else S1CampaignWorldRepository.isolated_dungeon_names()
        origins = select(Log.id).where(Log.target_dungeon.in_(maps))
        if world:
            return model.first_seen_log_id.in_(origins)
        return or_(model.first_seen_log_id.is_(None), model.first_seen_log_id.notin_(origins))

    @staticmethod
    def thread_scope(dungeon):
        """SQL boundary precedes relevance ranking, including closure recall."""
        from plugins.adventurer_s1.services.s1_campaign_world_service import campaign_world
        world = campaign_world(dungeon)
        if not world:
            # Activity memories never enter ordinary exploration either.
            maps = S1CampaignWorldRepository.isolated_dungeon_names()
            return or_(Thread.base_dungeon.is_(None), Thread.base_dungeon.notin_(maps))
        return Thread.base_dungeon.in_(world['dungeons'])
