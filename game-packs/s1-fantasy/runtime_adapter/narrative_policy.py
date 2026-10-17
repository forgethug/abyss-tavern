from runtime_core.narrative import BaseNarrativePackPolicy


def _neutral_status(value):
    status = str(value or "").strip()
    if status in {"active", "seeded", "escalated"}:
        return "active"
    if status in {"dormant", "rumored", "withdrawn", "missing"}:
        return "dormant"
    if status == "resolved":
        return "resolved"
    return "archived"


class S1NarrativePolicy(BaseNarrativePackPolicy):
    """Read-compatible projection of S1 authority without changing S1 tables."""

    pack_id = "s1_fantasy"

    def admin_descriptor(self):
        return {
            **super().admin_descriptor(),
            "labels": {
                "entity": "S1 世界角色",
                "fact": "S1 地点现状",
                "story_thread": "S1 故事线",
                "candidate": "共享维护候选",
                "snapshot": "共享注入快照",
            },
            "entity_kinds": ["individual", "faction", "organization"],
            "fact_kinds": ["location_state"],
            "thread_kinds": ["world", "personal"],
            "candidate_types": [],
            "supports_generic_writes": False,
            "compatibility_mode": "read_existing_s1_authority",
            "preview_mode": "administrative_projection_only",
            "custom_views": [
                {
                    "key": "s1_world_outline",
                    "label": "S1 世界大纲专用管理",
                    "path": "/play/{tavern_slug}",
                }
            ],
        }

    @staticmethod
    def external_entities(context=None):
        from plugins.adventurer_s1.models import AdventurerS1WorldActor

        rows = (
            AdventurerS1WorldActor.query.order_by(
                AdventurerS1WorldActor.importance.desc(),
                AdventurerS1WorldActor.id.asc(),
            )
            .limit(500)
            .all()
        )
        return [
            {
                "id": f"s1_world_actor:{row.id}",
                "external_key": f"s1_world_actor:{row.id}",
                "kind": row.actor_type,
                "name": row.canonical_name,
                "aliases": row.aliases or [],
                "summary": row.identity_summary or "",
                "status": _neutral_status(row.status),
                "visibility": "admin",
                "scope_keys": row.scope_keys or [],
                "importance": int(row.importance or 0),
                "revision": max(
                    1,
                    int(row.capability_revision or 0),
                ),
                "attributes": {
                    "s1_status": row.status,
                    "stable_principles": row.stable_principles or "",
                    "power_tier": row.power_tier or "",
                    "power_boundary": row.power_boundary or "",
                    "capability_profile": row.capability_profile or {},
                    "capability_revision": int(
                        row.capability_revision or 0
                    ),
                    "long_term_goal": row.long_term_goal or "",
                    "current_agenda": row.current_agenda or "",
                    "current_state": row.current_state or "",
                },
                "source_ref": f"adventurer_s1_world_actors:{row.id}",
            }
            for row in rows
        ]

    @staticmethod
    def external_facts(context=None):
        from plugins.adventurer_s1.models import (
            AdventurerS1WorldLocationState,
        )

        rows = (
            AdventurerS1WorldLocationState.query.order_by(
                AdventurerS1WorldLocationState.id.asc()
            )
            .limit(500)
            .all()
        )
        return [
            {
                "id": f"s1_location:{row.id}",
                "key": (
                    f"s1_location:{row.target_dungeon}:"
                    f"{row.canonical_name}"
                ),
                "kind": "location_state",
                "subject_key": row.canonical_name,
                "predicate": "current_state",
                "value": row.current_state,
                "summary": row.current_state,
                "status": "active",
                "visibility": "admin",
                "scope_keys": [row.target_dungeon],
                "revision": max(1, int(row.revision or 0)),
                "attributes": {
                    "last_changed_log_id": row.last_changed_log_id,
                },
                "source_ref": (
                    f"adventurer_s1_world_location_states:{row.id}"
                ),
            }
            for row in rows
        ]

    @staticmethod
    def external_story_threads(context=None):
        from plugins.adventurer_s1.models import (
            AdventurerS1StoryThread,
            AdventurerS1WorldStoryThread,
        )

        world_rows = (
            AdventurerS1WorldStoryThread.query.order_by(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .limit(500)
            .all()
        )
        personal_rows = (
            AdventurerS1StoryThread.query.order_by(
                AdventurerS1StoryThread.weight.desc(),
                AdventurerS1StoryThread.id.asc(),
            )
            .limit(500)
            .all()
        )
        result = [
            {
                "id": f"s1_world_thread:{row.id}",
                "external_key": f"s1_world_thread:{row.id}",
                "kind": row.thread_type,
                "title": row.title,
                "summary": row.summary or "",
                "next_hook": row.next_reveal_hint or "",
                "status": _neutral_status(row.status),
                "visibility": "admin",
                "scope_keys": [
                    *([row.base_dungeon] if row.base_dungeon else []),
                    *(row.scope_keys or []),
                ],
                "entity_refs": [],
                "attributes": {
                    "s1_status": row.status,
                    "trigger_keywords": row.trigger_keywords or [],
                    "power_tier": row.power_tier or "",
                    "power_profile": row.power_profile or "",
                    "closure_debt": int(row.closure_debt or 0),
                    "chain_key": row.chain_key,
                },
                "priority": int(row.weight or 0),
                "revision": max(1, int(row.revision or 1)),
                "injection_count": int(row.injection_count or 0),
                "source_ref": (
                    f"adventurer_s1_world_story_threads:{row.id}"
                ),
            }
            for row in world_rows
        ]
        result.extend(
            {
                "id": f"s1_personal_thread:{row.id}",
                "external_key": f"s1_personal_thread:{row.id}",
                "kind": "personal",
                "title": row.title,
                "summary": row.summary or "",
                "status": row.status,
                "visibility": "secret",
                "scope_keys": [
                    f"adventurer:{row.adventurer_id}"
                ],
                "entity_refs": (
                    row.entity_refs
                    if isinstance(row.entity_refs, list)
                    else list((row.entity_refs or {}).keys())
                ),
                "attributes": {
                    "s1_status": row.status,
                    "adventurer_id": row.adventurer_id,
                    "tags": row.tags or [],
                    "chain_key": row.chain_key,
                },
                "priority": int(row.weight or 0),
                "revision": 1,
                "source_ref": f"adventurer_s1_story_threads:{row.id}",
            }
            for row in personal_rows
        )
        return result


def create_policy(_app):
    return S1NarrativePolicy()
