"""Bounded identity-only projections; no actor or location authority is granted."""

from db_repository import db_manager


def load_dungeon_world_identity_guards(target_dungeon: str, *, limit: int) -> dict:
    from plugins.adventurer_s1.models import (
        AdventurerS1WorldActor,
        AdventurerS1WorldLocationState,
        AdventurerS1WorldStoryThread,
        AdventurerS1WorldStoryThreadActor,
    )

    if not target_dungeon or limit <= 0:
        return {'actors': [], 'locations': []}
    linked_actor_ids = db_manager.query(AdventurerS1WorldStoryThreadActor.actor_id).join(
        AdventurerS1WorldStoryThread,
        AdventurerS1WorldStoryThread.id == AdventurerS1WorldStoryThreadActor.world_story_thread_id,
    ).filter(AdventurerS1WorldStoryThread.base_dungeon == target_dungeon)
    actors = db_manager.query(
        AdventurerS1WorldActor.id,
        AdventurerS1WorldActor.canonical_name,
        AdventurerS1WorldActor.aliases,
    ).filter(AdventurerS1WorldActor.id.in_(linked_actor_ids)).order_by(
        AdventurerS1WorldActor.id,
    ).limit(limit + 1).all()
    locations = db_manager.query(
        AdventurerS1WorldLocationState.id,
        AdventurerS1WorldLocationState.canonical_name,
    ).filter(AdventurerS1WorldLocationState.target_dungeon == target_dungeon).order_by(
        AdventurerS1WorldLocationState.id,
    ).limit(limit + 1).all()
    return {
        'actors': [
            {
                'id': int(actor_id), 'canonical_name': name,
                'aliases': list(aliases) if isinstance(aliases, list) else [],
            }
            for actor_id, name, aliases in actors[:limit]
        ],
        'locations': [
            {'location_id': int(location_id), 'location_name': name}
            for location_id, name in locations[:limit]
        ],
        'has_more_actors': len(actors) > limit,
        'has_more_locations': len(locations) > limit,
    }
