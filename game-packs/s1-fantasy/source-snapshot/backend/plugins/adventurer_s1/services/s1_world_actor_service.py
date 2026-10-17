"""S1 长期世界角色：有界当前态、描述式势力关系与世界线关联。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1WorldActor,
    AdventurerS1WorldActorFactionRelation,
    AdventurerS1WorldActorInteraction,
    AdventurerS1WorldStoryThread,
    AdventurerS1WorldStoryThreadActor,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)
from plugins.adventurer_s1.services.s1_world_outline_contract import (
    MAX_WORLD_FACT_CONTAINER_ITEMS,
)

ACTOR_TYPES = frozenset({'individual', 'collective', 'manifestation', 'other'})
ACTOR_STATUSES = frozenset({'rumored', 'active', 'withdrawn', 'missing', 'retired', 'dead'})
INVOLVEMENT_TYPES = frozenset({'direct', 'indirect', 'historical'})


def _enabled() -> bool:
    return bool(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_ENABLED', True))


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clip(value, limit: int) -> str:
    if not isinstance(value, str):
        return ''
    return value.strip()[:max(0, int(limit))]


def _field_limit() -> int:
    return max(80, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_FIELD_MAX_CHARS'), 320))


def _relation_field_limit() -> int:
    return max(60, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_RELATION_FIELD_MAX_CHARS'), 220))


def _text_list(value, *, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        text = _clip(item, item_chars)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _find_actor(raw: dict) -> AdventurerS1WorldActor | None:
    raw_actor_id = raw.get('id') if 'id' in raw else raw.get('actor_id')
    actor_id, _ = normalize_ascii_integer(raw_actor_id, minimum=1)
    if type(actor_id) is int and actor_id > 0:
        return AdventurerS1WorldActor.query.filter_by(id=actor_id).with_for_update().populate_existing().first()
    return None


def _upsert_relation(actor_id: int, raw: dict, source_log_id: int | None) -> None:
    faction_name = _clip(raw.get('faction_name'), 160)
    faction_key = _clip(raw.get('faction_key'), 120)
    if not faction_name or not faction_key:
        return
    row = AdventurerS1WorldActorFactionRelation.query.filter_by(
        actor_id=actor_id,
        faction_key=faction_key,
    ).with_for_update().first()
    if not row:
        row = AdventurerS1WorldActorFactionRelation(
            actor_id=actor_id,
            faction_key=faction_key,
            faction_name=faction_name,
        )
        db_manager.add(row, flush=False)
    row.faction_name = faction_name
    field_limit = _relation_field_limit()
    for field in (
        'public_stance', 'private_view', 'interests', 'concerns', 'dependencies',
        'unresolved_matters', 'behavior_boundary', 'last_change_summary',
    ):
        if field in raw:
            setattr(row, field, _clip(raw.get(field), field_limit) or None)
    row.source_log_id = source_log_id or row.source_log_id


def _link_actor_to_thread(actor_id: int, raw: dict) -> int | None:
    raw_thread_id = (
        raw.get('thread_id')
        if 'thread_id' in raw
        else raw.get('world_story_thread_id')
    )
    has_thread_id = 'thread_id' in raw or 'world_story_thread_id' in raw
    thread_id, _ = normalize_ascii_integer(raw_thread_id, minimum=1)
    if has_thread_id and (type(thread_id) is not int or thread_id <= 0):
        return None
    raw_thread_title = raw.get('thread_title')
    thread_title = (
        raw_thread_title.strip()[:120]
        if isinstance(raw_thread_title, str)
        else ''
    )
    if has_thread_id and thread_title:
        return None
    thread = None
    if has_thread_id:
        thread = AdventurerS1WorldStoryThread.query.filter_by(id=thread_id).first()
    elif thread_title:
        thread = AdventurerS1WorldStoryThread.query.filter_by(title=thread_title).first()
    if not thread:
        return None
    raw_involvement = raw.get('involvement')
    involvement = (
        raw_involvement.strip()
        if isinstance(raw_involvement, str)
        else ''
    )
    if involvement not in INVOLVEMENT_TYPES:
        return None
    if 'lease_required' not in raw:
        return None
    lease_required, _ = normalize_exact_boolean(raw.get('lease_required'))
    if type(lease_required) is not bool:
        return None
    row = AdventurerS1WorldStoryThreadActor.query.filter_by(
        world_story_thread_id=thread.id,
        actor_id=actor_id,
    ).with_for_update().first()
    if not row:
        row = AdventurerS1WorldStoryThreadActor(
            world_story_thread_id=thread.id,
            actor_id=actor_id,
        )
        db_manager.add(row, flush=False)
    row.involvement = involvement
    # This exact role is issued by encounter preparation, never by AI maintenance.
    # Preserve its archive identity while the ordinary relation continues to evolve.
    if row.narrative_role != 'encounter_pool':
        role = _clip(raw.get('narrative_role'), 40) or None
        row.narrative_role = None if role == 'encounter_pool' else role
    row.lease_required = lease_required and involvement == 'direct'
    return int(thread.id)


def apply_maintenance_updates(
    raw_actors,
    *,
    candidate_ids: list[int],
    last_seen_log_id: int | None,
    capability_bases: dict | None = None,
) -> dict:
    """应用维护 AI 的角色当前态；无效项静默丢弃，不影响世界线与探险结算。"""
    if not _enabled() or not current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_AUTO_MAINTENANCE_ENABLED', True):
        return {'created': 0, 'updated': 0, 'interactions': 0, 'missing_profile_actor_ids': []}
    if not isinstance(raw_actors, list):
        return {'created': 0, 'updated': 0, 'interactions': 0, 'missing_profile_actor_ids': []}
    max_updates = min(
        MAX_WORLD_FACT_CONTAINER_ITEMS,
        max(0, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_MAX_AI_UPDATES'),
            3,
        )),
    )
    created = updated = interactions = 0
    created_actor_ids = {}
    deferred_capability_actor_ids = []
    missing_profile_actor_ids: list[int] = []
    if len(raw_actors) > max_updates:
        return {
            'created': 0,
            'updated': 0,
            'interactions': 0,
            'missing_profile_actor_ids': [],
        }
    for raw_index, raw in enumerate(raw_actors):
        if not isinstance(raw, dict):
            continue
        has_explicit_id = 'id' in raw or 'actor_id' in raw
        raw_actor_id = raw.get('id') if 'id' in raw else raw.get('actor_id')
        actor_id, _ = normalize_ascii_integer(raw_actor_id, minimum=1)
        if has_explicit_id and (type(actor_id) is not int or actor_id <= 0):
            continue
        if (
            'actor_type' in raw
            and (
                not isinstance(raw.get('actor_type'), str)
                or raw['actor_type'].strip() not in ACTOR_TYPES
            )
        ):
            continue
        if (
            'status' in raw
            and (
                not isinstance(raw.get('status'), str)
                or raw['status'].strip() not in ACTOR_STATUSES
            )
        ):
            continue
        actor = _find_actor(raw)
        if has_explicit_id and actor is None:
            # Existing-actor updates are identity-bound. An unknown explicit ID
            # must never be reinterpreted as authority to create a new actor.
            continue
        is_new = actor is None
        name = _clip(raw.get('canonical_name') or raw.get('name'), 120)
        identity = _clip(raw.get('identity_summary'), _field_limit())
        if is_new:
            from db_repository.s1_world_actor_identity_repository import actor_name_exists
            if (
                not name
                or not identity
                or actor_name_exists(name)
            ):
                continue
            actor = AdventurerS1WorldActor(canonical_name=name, identity_summary=identity)
            db_manager.add(actor, flush=True)
            created += 1
            created_actor_ids[raw_index] = int(actor.id)
        else:
            updated += 1
        base = (capability_bases or {}).get(str(actor.id))
        capability_current = is_new or capability_bases is None or (
            isinstance(base, dict)
            and actor.capability_revision == base.get('capability_revision')
            and actor.capability_profile == base.get('capability_profile')
            and (actor.power_boundary or '') == (base.get('power_boundary') or '')
        )
        if not capability_current and any(k in raw for k in ('capability_profile', 'power_boundary', 'power_tier')):
            deferred_capability_actor_ids.append(int(actor.id))
        raw_actor_type = raw.get('actor_type')
        actor_type = raw_actor_type.strip() if isinstance(raw_actor_type, str) else ''
        raw_status = raw.get('status')
        status = raw_status.strip() if isinstance(raw_status, str) else ''
        if actor_type not in ACTOR_TYPES or status not in ACTOR_STATUSES:
            continue
        actor.actor_type = actor_type
        actor.status = status
        if identity:
            actor.identity_summary = identity
        actor.aliases = _text_list(raw.get('aliases'), limit=8, item_chars=120) or actor.aliases
        actor.scope_keys = _text_list(raw.get('scope_keys'), limit=8, item_chars=80) or actor.scope_keys
        actor.trigger_keywords = _text_list(raw.get('trigger_keywords'), limit=10, item_chars=50) or actor.trigger_keywords
        for field in ('stable_principles', 'power_boundary', 'long_term_goal', 'current_agenda', 'current_state'):
            if field == 'power_boundary' and not capability_current:
                continue
            if field in raw:
                setattr(actor, field, _clip(raw.get(field), _field_limit()) or None)
        if 'power_tier' in raw and capability_current:
            actor.power_tier = _clip(raw.get('power_tier'), 30) or None
        if 'capability_profile' in raw and capability_current:
            from plugins.adventurer_s1.services.s1_capability_profile_contract import (
                normalize_actor_capability_profile,
            )
            capability_profile = normalize_actor_capability_profile(raw.get('capability_profile'))
            if capability_profile is not None and capability_profile != (actor.capability_profile or None):
                actor.capability_profile = capability_profile
                actor.capability_revision = max(0, int(actor.capability_revision or 0)) + 1
                actor.capability_source_log_id = last_seen_log_id
        normalized_importance, _ = normalize_ascii_integer(
            raw.get('importance'),
            minimum=1,
            maximum=100,
        )
        if type(normalized_importance) is int:
            actor.importance = normalized_importance
        actor.source_candidate_ids = list(dict.fromkeys(
            list(actor.source_candidate_ids or []) + [int(x) for x in candidate_ids]
        ))[-50:]
        actor.first_seen_log_id = actor.first_seen_log_id or last_seen_log_id
        actor.last_seen_log_id = last_seen_log_id or actor.last_seen_log_id
        actor.last_active_at = _portable_utc_now()
        db_manager.flush()
        if (
            actor.status == 'active'
            and (not actor.capability_profile or int(actor.capability_revision or 0) <= 0)
        ):
            missing_profile_actor_ids.append(int(actor.id))
        for relation in (raw.get('faction_relations') or [])[:6]:
            if isinstance(relation, dict):
                _upsert_relation(actor.id, relation, last_seen_log_id)
        linked_thread_id = None
        for link in (raw.get('thread_links') or [])[:4]:
            if isinstance(link, dict):
                linked_thread_id = _link_actor_to_thread(actor.id, link) or linked_thread_id
        interaction_summary = _clip(raw.get('interaction_summary'), _field_limit())
        if interaction_summary and last_seen_log_id:
            existing = AdventurerS1WorldActorInteraction.query.filter_by(
                actor_id=actor.id,
                expedition_log_id=last_seen_log_id,
                world_story_thread_id=linked_thread_id,
            ).first()
            if not existing:
                db_manager.add(AdventurerS1WorldActorInteraction(
                    actor_id=actor.id,
                    expedition_log_id=last_seen_log_id,
                    world_story_thread_id=linked_thread_id,
                    interaction_summary=interaction_summary,
                    actor_change_summary=_clip(raw.get('actor_change_summary'), _field_limit()) or None,
                    relation_change_summary=_clip(raw.get('relation_change_summary'), _field_limit()) or None,
                ), flush=False)
                interactions += 1
    return {
        'created': created,
        'updated': updated,
        'interactions': interactions,
        'missing_profile_actor_ids': list(dict.fromkeys(missing_profile_actor_ids)),
        'created_actor_ids': created_actor_ids,
        'deferred_capability_actor_ids': deferred_capability_actor_ids,
    }


def _actor_prompt_core(
    actor, mutable_ids: set[int], links: list, *, terminal_guard: bool = False,
    include_current_context: bool = True,
):
    """Keep identity, capability and actual current action/state together."""
    terminal = actor.status in ('dead', 'retired')
    direct_thread_ids = [
        int(link.world_story_thread_id) for link in links
        if int(link.actor_id) == int(actor.id)
        and int(link.world_story_thread_id) in mutable_ids and bool(link.lease_required)
    ]
    actor_access = (
        '权威终态·只读' if terminal else '可直接推进' if direct_thread_ids else '只读当前态'
    )
    allowed_modes = ['ignored', 'background_reference']
    if not terminal and direct_thread_ids and actor.capability_profile and int(actor.capability_revision or 0) > 0:
        allowed_modes.append('exclusive_direct_use')
    lines = [
        f'[{actor_access}] [world_actor_id:{actor.id}] '
        f'[capability_revision:{int(actor.capability_revision or 0)}] '
        f'{actor.canonical_name}（{actor.status}）',
        f"本次 usage_mode 允许值：{' / '.join(allowed_modes)}",
    ]
    if terminal_guard:
        return lines, actor_access, allowed_modes, direct_thread_ids
    lines.append(f'身份：{actor.identity_summary}')
    if 'exclusive_direct_use' in allowed_modes:
        lines.append(
            '本体直接使用组合：该角色选择 exclusive_direct_use 时，'
            'world_intel_usages 中以下 world_story_thread_id 至少一项同步选择 '
            f'exclusive_direct_use：{direct_thread_ids}'
        )
    if not terminal and actor.capability_profile and int(actor.capability_revision or 0) > 0:
        lines.append('权威能力画像：' + json.dumps(
            actor.capability_profile, ensure_ascii=False, separators=(',', ':'),
        ))
    if include_current_context and not terminal and actor.current_agenda:
        lines.append(f'当前行动：{actor.current_agenda}')
    if include_current_context and actor.current_state:
        lines.append(f'当前处境：{actor.current_state}')
    return lines, actor_access, allowed_modes, direct_thread_ids


def render_actor_selection_prompt_block(entry: dict, actor_state: dict) -> str:
    """Project selection context while retaining full knowledge for adoption."""
    original = str(entry.get('prompt_block') or '')
    allowed_modes = actor_state.get('allowed_usage_modes')
    if (
        isinstance(allowed_modes, list)
        and 'exclusive_direct_use' in allowed_modes
        and all(type(actor_state.get(key)) is int and actor_state[key] > 0
                for key in ('id', 'capability_revision'))
        and all(type(entry.get(key)) is int and entry[key] > 0
                for key in ('world_actor_id', 'capability_revision'))
        and actor_state.get('status') not in ('dead', 'retired')
        and actor_state.get('id') == entry.get('world_actor_id')
        and actor_state.get('capability_revision') == entry.get('capability_revision')
    ):
        from .s1_countermeasure_knowledge_service import COUNTERMEASURE_PROMPT_TEMPLATE_SLOT
        # Omit only the frozen knowledge slot. Identity, current capability and
        # usage authority stay in the selection view; the full entry is retained.
        template = entry.get('countermeasure_prompt_template')
        if isinstance(template, str) and template.count(COUNTERMEASURE_PROMPT_TEMPLATE_SLOT) == 1:
            projected = template.replace(COUNTERMEASURE_PROMPT_TEMPLATE_SLOT, '').strip()
            if projected and len(projected) <= len(original):
                return projected
    if (
        not isinstance(allowed_modes, list)
        or not allowed_modes
        or 'exclusive_direct_use' in allowed_modes
        or actor_state.get('status') in ('dead', 'retired')
        or actor_state.get('id') != entry.get('world_actor_id')
        or actor_state.get('capability_revision') != entry.get('capability_revision')
        or not all(key in actor_state for key in (
            'canonical_name', 'identity_summary', 'current_state',
        ))
    ):
        return original
    projected = '\n'.join((
        f"[只读当前态] [world_actor_id:{actor_state['id']}] "
        f"[capability_revision:{actor_state['capability_revision']}] "
        f"{actor_state['canonical_name']}（{actor_state.get('status') or ''}）",
        f"本次 usage_mode 允许值：{' / '.join(allowed_modes)}",
        f"身份：{actor_state['identity_summary']}",
        f"当前处境：{actor_state['current_state']}",
    ))
    # Selection already reserved the original block's budget, including terminal
    # guards. A previously omitted long state must not enlarge that reservation.
    return projected if len(projected) <= len(original) else original


def actor_prompt_header() -> str:
    return (
        '【相关长期世界角色（结构化权威当前态）】\n'
        'status、当前行动与当前处境高于接力便签、公共世界线和自由创作；冲突资料只能保留兼容的历史与后果。\n'
        'status=dead 表示主体已经死亡，只能作为历史事实或既有后果被提及，不得重新执行行动、现身互动或恢复。只读角色不得改变当前态。\n'
        '长期角色本体使用采用完整授权组合：角色条目允许 exclusive_direct_use、至少一条该角色绑定的可推进公共线同步选择 exclusive_direct_use；准确 world_actor_usages 由服务端绑定该角色唯一的冻结权威画像。既有本领直接参与当前局势，画像不按本次副本等级或队伍等级重算。\n'
    )


def actor_prompt_core_budget() -> int:
    return max(400, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_WORLD_ACTOR_PROMPT_TOTAL_CHARS'), 24000)) - len(actor_prompt_header())


def build_actor_prompt_block(
    thread_ids: list[int],
    *,
    mutable_thread_ids: set[int] | None = None,
    priority_actor_ids: list[int] | None = None,
    excluded_actor_ids: set[int] | None = None,
    authority_reference_texts: list[str] | None = None,
    prompt_snapshot_out: dict | None = None,
    activity_dungeons: list[str] | None = None,
) -> tuple[str, list[int], list[str], list[dict]]:
    """为已经命中的世界线构建固定预算角色快照；完整历史永不注入。"""
    if not _enabled() or (not thread_ids and not authority_reference_texts):
        return '', [], [], []
    links = []
    if thread_ids:
        links = AdventurerS1WorldStoryThreadActor.query.filter(
            AdventurerS1WorldStoryThreadActor.world_story_thread_id.in_(thread_ids),
        ).order_by(AdventurerS1WorldStoryThreadActor.lease_required.desc()).all()
    actor_ids = list(dict.fromkeys(int(link.actor_id) for link in links))
    exact_reference_actor_ids: set[int] = set()
    reference_texts = [
        str(value).strip() for value in (authority_reference_texts or [])
        if str(value or '').strip()
    ]
    if reference_texts:
        from db_repository.s1_world_actor_identity_repository import referenced_actor_ids
        exact_reference_actor_ids = referenced_actor_ids(reference_texts)
        actor_ids.extend(sorted(exact_reference_actor_ids))
        actor_ids = list(dict.fromkeys(actor_ids))
    excluded_ids = {int(value) for value in (excluded_actor_ids or set())}
    if activity_dungeons is not None and actor_ids:
        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
        allowed = S1CampaignWorldRepository.owned_actor_ids(actor_ids, activity_dungeons)
        actor_ids = [actor_id for actor_id in actor_ids if actor_id in allowed]
        exact_reference_actor_ids.intersection_update(allowed)
    if excluded_ids:
        actor_ids = [actor_id for actor_id in actor_ids if actor_id not in excluded_ids]
        exact_reference_actor_ids.difference_update(excluded_ids)
    if not actor_ids:
        return '', [], [], []
    mutable_ids = {int(value) for value in (mutable_thread_ids or set())}
    mutable_actor_ids = {
        int(link.actor_id) for link in links
        if int(link.world_story_thread_id) in mutable_ids and bool(link.lease_required)
    }
    limit = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_PROMPT_LIMIT'), 3))
    actors = AdventurerS1WorldActor.query.filter(
        AdventurerS1WorldActor.id.in_(actor_ids),
    ).all()
    terminal_statuses = {'dead', 'retired'}
    direct_ready_actor_ids = {
        int(actor.id) for actor in actors
        if (
            actor.status not in terminal_statuses
            and int(actor.id) in mutable_actor_ids
            and actor.capability_profile
            and int(actor.capability_revision or 0) > 0
        )
    }
    priority_rank = {
        int(actor_id): rank
        for rank, actor_id in enumerate(priority_actor_ids or [])
        if int(actor_id or 0) > 0
    }
    actors.sort(key=lambda actor: (
        0 if int(actor.id) in direct_ready_actor_ids
        else (1 if actor.status in terminal_statuses else 2),
        (
            priority_rank.get(int(actor.id), len(priority_rank))
            if int(actor.id) in direct_ready_actor_ids
            else 0
        ),
        -int(actor.importance or 0),
        int(actor.id),
    ))
    actor_candidates = actors
    actors = actor_candidates[:limit]
    terminal_guard_ids: set[int] = set()
    if any(
        int(actor.id) in direct_ready_actor_ids
        for actor in actors
    ):
        terminal_guard = next(
            (
                actor for actor in actor_candidates
                if actor.status in terminal_statuses and actor not in actors
            ),
            None,
        )
        if terminal_guard is not None:
            actors.append(terminal_guard)
            terminal_guard_ids.add(int(terminal_guard.id))
    compact_terminal_guard_ids = set(terminal_guard_ids)
    # A terminal actor already inside the normal capacity needs the same
    # minimum authority reservation as an appended terminal guard.
    terminal_guard_ids.update(
        int(actor.id) for actor in actors if actor.status in terminal_statuses
    )
    if not actors:
        return '', [], [], []
    relation_limit = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_RELATION_LIMIT'), 3))
    relations_by_actor: dict[int, list[AdventurerS1WorldActorFactionRelation]] = {
        int(actor.id): [] for actor in actors
    }
    if relation_limit > 0:
        relation_rows = AdventurerS1WorldActorFactionRelation.query.filter(
            AdventurerS1WorldActorFactionRelation.actor_id.in_(relations_by_actor),
        ).order_by(
            AdventurerS1WorldActorFactionRelation.actor_id,
            AdventurerS1WorldActorFactionRelation.updated_at.desc(),
        ).all()
        for relation in relation_rows:
            actor_relations = relations_by_actor.get(int(relation.actor_id))
            if actor_relations is not None and len(actor_relations) < relation_limit:
                actor_relations.append(relation)
    total_chars = max(400, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_PROMPT_TOTAL_CHARS'), 24000))
    header = actor_prompt_header()
    if isinstance(prompt_snapshot_out, dict):
        prompt_snapshot_out['header'] = header
        prompt_snapshot_out['entries'] = []
        prompt_snapshot_out['exact_reference_match_ids'] = sorted(exact_reference_actor_ids)
    blocks = []
    selection_blocks = []
    included_ids = []
    included_faction_keys: list[str] = []
    authority_states: list[dict] = []
    core_by_actor = {
        actor.id: _actor_prompt_core(
            actor, mutable_ids, links, terminal_guard=actor.id in terminal_guard_ids,
            include_current_context=not (
                actor.id in priority_rank and actor.status not in terminal_statuses
            ),
        ) for actor in actors
    }
    terminal_guard_reserve = sum(
        len('\n'.join(core_by_actor[actor.id][0])) + 2
        for actor in actors if actor.id in terminal_guard_ids
    )
    terminal_remaining = max(0, total_chars - len(header))
    # Reserve each admitted actor's complete core before the first actor can
    # spend the remaining budget on relations, principles or learning details.
    core_budget = total_chars - len(header) - terminal_guard_reserve
    admitted = []
    priority_knowledge = {}
    for actor in actors:
        core = core_by_actor[actor.id][0]
        size = len('\n'.join(core)) + 2
        if actor.id in terminal_guard_ids:
            if size <= terminal_remaining:
                admitted.append(actor)
                terminal_remaining -= size
        elif size <= core_budget:
            if actor.id in priority_rank and actor.status not in terminal_statuses:
                # Preserve the existing due-learning priority before admitting
                # more actors or spending space on optional current prose.
                from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
                    build_countermeasure_prompt_block,
                )
                knowledge_ids, knowledge_refs, candidate_refs = [], [], []
                knowledge_block = build_countermeasure_prompt_block(
                    actor, relations_by_actor.get(int(actor.id), []),
                    max_chars=max(0, core_budget - size - 1),
                    knowledge_ids_out=knowledge_ids,
                    knowledge_refs_out=knowledge_refs,
                    knowledge_candidates_out=candidate_refs,
                )
                if knowledge_block:
                    priority_knowledge[actor.id] = (
                        knowledge_block, knowledge_ids, knowledge_refs, candidate_refs, len(core),
                    )
                    core.append(knowledge_block)
                    size = len('\n'.join(core)) + 2
            admitted.append(actor)
            core_budget -= size
    actors = admitted
    for actor_index, actor in enumerate(actors):
        relations = relations_by_actor.get(int(actor.id), [])
        remaining = total_chars - len(header) - sum(len(x) for x in blocks) - max(0, len(blocks)) * 2
        is_terminal_guard = int(actor.id) in terminal_guard_ids
        remaining -= sum(
            len('\n'.join(core_by_actor[other.id][0])) + 2
            for other in actors[actor_index + 1:]
        )
        core, actor_access, allowed_usage_modes, direct_thread_ids = core_by_actor[actor.id]
        lines = list(core)
        if is_terminal_guard and actor.id not in compact_terminal_guard_ids:
            full_terminal_core = _actor_prompt_core(actor, mutable_ids, links)[0]
            if len('\n'.join(full_terminal_core)) <= remaining:
                lines = full_terminal_core
                is_terminal_guard = False
        countermeasure_block = ''
        countermeasure_knowledge_ids: list[int] = []
        countermeasure_knowledge_refs: list[dict] = []
        countermeasure_candidate_refs: list[dict] = []
        countermeasure_insert_index: int | None = None
        countermeasure_prompt_slot = ''
        is_priority_countermeasure_actor = bool(
            actor.status not in terminal_statuses
            and int(actor.id) in priority_rank
        )
        if is_priority_countermeasure_actor:
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
                COUNTERMEASURE_PROMPT_TEMPLATE_SLOT,
            )
            countermeasure_prompt_slot = COUNTERMEASURE_PROMPT_TEMPLATE_SLOT
            (
                countermeasure_block, countermeasure_knowledge_ids,
                countermeasure_knowledge_refs, countermeasure_candidate_refs,
                countermeasure_insert_index,
            ) = priority_knowledge.get(
                actor.id, ('', [], [], [], None),
            )
            for label, value in (
                ('当前行动', actor.current_agenda), ('当前处境', actor.current_state),
            ):
                if value:
                    line = f'{label}：{value}'
                    if len('\n'.join(lines + [line])) <= remaining:
                        lines.append(line)
        if (
            actor.status not in terminal_statuses
            and not is_priority_countermeasure_actor
        ):
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
                COUNTERMEASURE_PROMPT_TEMPLATE_SLOT,
                build_countermeasure_prompt_block,
            )
            countermeasure_prompt_slot = COUNTERMEASURE_PROMPT_TEMPLATE_SLOT
            available_chars = max(0, remaining - len('\n'.join(lines)) - 1)
            countermeasure_block = build_countermeasure_prompt_block(
                actor,
                relations,
                max_chars=available_chars,
                knowledge_ids_out=countermeasure_knowledge_ids,
                knowledge_refs_out=countermeasure_knowledge_refs,
                knowledge_candidates_out=countermeasure_candidate_refs,
            )
        if countermeasure_block and not is_priority_countermeasure_actor:
            countermeasure_insert_index = len(lines)
            lines.append(countermeasure_block)
        for label, value in (
            ()
            if actor.status in terminal_statuses
            else (
                ('稳定原则', actor.stable_principles),
                ('力量边界', actor.power_boundary),
                ('长期目标', actor.long_term_goal),
            )
        ):
            if value:
                line = f'{label}：{value}'
                if len('\n'.join(lines + [line])) <= remaining:
                    lines.append(line)
        for relation in (() if actor.status in terminal_statuses else relations):
            prefix = f"对势力[faction_key:{relation.faction_key}] {relation.faction_name}："
            fact_parts = []
            for key, value in relation.to_prompt_dict().items():
                if key == 'faction' or not value:
                    continue
                candidate_part = f'{key}={value}'
                line = prefix + '；'.join(fact_parts + [candidate_part])
                if len('\n'.join(lines + [line])) > remaining:
                    break
                fact_parts.append(candidate_part)
            if fact_parts:
                lines.append(prefix + '；'.join(fact_parts))
                if relation.faction_key not in included_faction_keys:
                    included_faction_keys.append(relation.faction_key)
        candidate = '\n'.join(lines)
        countermeasure_prompt_template = ''
        if (
            countermeasure_insert_index is not None
            and countermeasure_prompt_slot
        ):
            template_lines = list(lines)
            template_lines[
                countermeasure_insert_index
            ] = countermeasure_prompt_slot
            countermeasure_prompt_template = '\n'.join(template_lines)
        blocks.append(candidate)
        included_ids.append(int(actor.id))
        if isinstance(prompt_snapshot_out, dict):
            prompt_entry = {
                'world_actor_id': int(actor.id),
                'capability_revision': int(actor.capability_revision or 0),
                'status': actor.status,
                'exact_reference_match': int(actor.id) in exact_reference_actor_ids,
                'countermeasure_knowledge_ids': countermeasure_knowledge_ids,
                'countermeasure_knowledge_refs': countermeasure_knowledge_refs,
                'prompt_block': candidate,
            }
            if countermeasure_prompt_template and countermeasure_candidate_refs:
                # Objective-outline AI only receives prompt_block. These internal
                # fields freeze the larger eligible set so main generation can
                # reprioritize it later without re-reading mutable world state.
                prompt_entry['countermeasure_candidate_refs'] = (
                    countermeasure_candidate_refs
                )
                prompt_entry['countermeasure_prompt_template'] = (
                    countermeasure_prompt_template
                )
                prompt_entry['countermeasure_prompt_max_chars'] = len(
                    countermeasure_block,
                )
            if is_terminal_guard:
                prompt_entry['terminal_guard'] = True
            prompt_snapshot_out['entries'].append(prompt_entry)
        authority_state = {
            'id': int(actor.id),
            'canonical_name': actor.canonical_name,
            'aliases': list(actor.aliases or []),
            'status': actor.status,
            'identity_summary': actor.identity_summary or '',
            'power_tier': actor.power_tier or '',
            'power_boundary': actor.power_boundary or '',
            'capability_profile': actor.capability_profile,
            'capability_revision': int(actor.capability_revision or 0),
            'faction_keys': [
                str(relation.faction_key)
                for relation in relations
                if str(relation.faction_key or '').strip()
            ],
            'direct_world_story_thread_ids': direct_thread_ids,
            'current_agenda': actor.current_agenda or '',
            'current_state': actor.current_state or '',
            'access_mode': actor_access,
            'allowed_usage_modes': allowed_usage_modes,
        }
        authority_states.append(authority_state)
        selection_block = render_actor_selection_prompt_block({
            'world_actor_id': int(actor.id),
            'capability_revision': int(actor.capability_revision or 0),
            'prompt_block': candidate,
            'countermeasure_prompt_template': countermeasure_prompt_template,
        }, authority_state)
        selection_blocks.append(selection_block)
        if isinstance(prompt_snapshot_out, dict):
            prompt_entry['selection_prompt_block'] = selection_block
    if not blocks:
        return '', [], [], []
    return header + '\n\n'.join(selection_blocks), included_ids, included_faction_keys, authority_states


def build_maintenance_actor_context(
    thread_ids: list[int],
    *,
    required_actor_ids: list[int] | None = None,
    target_dungeon: str | None = None,
) -> list[dict]:
    """给后置维护 AI 的有界角色当前态；冻结目标可脱离旧线单独进入。"""
    if not _enabled() or (not thread_ids and not required_actor_ids):
        return []
    normalized_thread_ids = list(dict.fromkeys(
        int(value) for value in thread_ids if int(value or 0) > 0
    ))
    required_ids = list(dict.fromkeys(
        int(value)
        for value in (required_actor_ids or [])
        if int(value or 0) > 0
    ))
    links = (
        AdventurerS1WorldStoryThreadActor.query.filter(
            AdventurerS1WorldStoryThreadActor.world_story_thread_id.in_(
                normalized_thread_ids,
            ),
        ).all()
        if normalized_thread_ids else []
    )
    actor_ids = list(dict.fromkeys([
        *required_ids,
        *(int(link.actor_id) for link in links),
    ]))
    limit = max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_MAX_AI_UPDATES'), 3))
    actor_query = AdventurerS1WorldActor.query.filter(
        AdventurerS1WorldActor.id.in_(actor_ids),
    )
    if target_dungeon:
        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
        actor_query = actor_query.filter(S1CampaignWorldRepository.entity_scope(AdventurerS1WorldActor, target_dungeon))
    actors = actor_query.all()
    required_rank = {actor_id: index for index, actor_id in enumerate(required_ids)}
    actors.sort(key=lambda actor: (
        int(actor.id) not in required_rank,
        required_rank.get(int(actor.id), len(required_rank)),
        -int(actor.importance or 0),
        int(actor.id),
    ))
    actors = actors[:limit]
    selected_actor_ids = [int(actor.id) for actor in actors]
    relations = (
        AdventurerS1WorldActorFactionRelation.query.filter(
            AdventurerS1WorldActorFactionRelation.actor_id.in_(
                selected_actor_ids,
            ),
        ).order_by(
            AdventurerS1WorldActorFactionRelation.actor_id.asc(),
            AdventurerS1WorldActorFactionRelation.updated_at.desc(),
            AdventurerS1WorldActorFactionRelation.id.asc(),
        ).all()
        if selected_actor_ids else []
    )
    relations_by_actor: dict[int, list[AdventurerS1WorldActorFactionRelation]] = {}
    for relation in relations:
        bucket = relations_by_actor.setdefault(int(relation.actor_id), [])
        if len(bucket) < 6:
            bucket.append(relation)
    result = []
    for actor in actors:
        data = actor.to_admin_dict()
        data['faction_relations'] = [
            row.to_prompt_dict()
            for row in relations_by_actor.get(int(actor.id), [])
        ]
        data['linked_thread_ids'] = [
            int(link.world_story_thread_id) for link in links if int(link.actor_id) == int(actor.id)
        ]
        result.append(data)
    return result


def build_actor_name_registry(*, priority_actor_ids=()) -> list[dict]:
    """给维护 AI 的有界命名提示；完整历史身份在写入时独立核对。"""
    if not _enabled():
        return []
    from db_repository.s1_world_actor_identity_repository import prompt_name_registry
    return prompt_name_registry(priority_actor_ids=priority_actor_ids)


def list_world_actors(*, page: int, per_page: int, status: str | None = None, keyword: str | None = None) -> dict:
    query = AdventurerS1WorldActor.query
    if status:
        query = query.filter(AdventurerS1WorldActor.status == status)
    if keyword:
        query = query.filter(AdventurerS1WorldActor.canonical_name.ilike(f'%{keyword[:120]}%'))
    pagination = query.order_by(
        AdventurerS1WorldActor.importance.desc(),
        AdventurerS1WorldActor.updated_at.desc(),
    ).paginate(page=page, per_page=per_page, error_out=False)
    return {
        'items': [actor.to_admin_dict() for actor in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'pages': pagination.pages,
    }


def get_world_actor_detail(actor_id: int) -> tuple[dict | None, str | None]:
    actor = AdventurerS1WorldActor.query.filter_by(id=int(actor_id)).first()
    if not actor:
        return None, '世界角色不存在'
    data = actor.to_admin_dict()
    data['faction_relations'] = [
        row.to_prompt_dict() | {'id': row.id, 'faction_key': row.faction_key, 'source_log_id': row.source_log_id}
        for row in AdventurerS1WorldActorFactionRelation.query.filter_by(actor_id=actor.id).order_by(
            AdventurerS1WorldActorFactionRelation.updated_at.desc(),
        ).all()
    ]
    links = AdventurerS1WorldStoryThreadActor.query.filter_by(actor_id=actor.id).all()
    thread_map = {
        int(row.id): row for row in AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.id.in_([int(link.world_story_thread_id) for link in links]),
        ).all()
    } if links else {}
    data['world_story_threads'] = [
        {
            'id': link.world_story_thread_id,
            'title': thread_map.get(int(link.world_story_thread_id)).title if thread_map.get(int(link.world_story_thread_id)) else '',
            'involvement': link.involvement,
            'narrative_role': link.narrative_role or '',
            'lease_required': bool(link.lease_required),
        }
        for link in links
    ]
    data['recent_interactions'] = [
        {
            'id': row.id,
            'expedition_log_id': row.expedition_log_id,
            'world_story_thread_id': row.world_story_thread_id,
            'interaction_summary': row.interaction_summary,
            'actor_change_summary': row.actor_change_summary or '',
            'relation_change_summary': row.relation_change_summary or '',
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }
        for row in AdventurerS1WorldActorInteraction.query.filter_by(actor_id=actor.id).order_by(
            AdventurerS1WorldActorInteraction.created_at.desc(),
        ).limit(20).all()
    ]
    return data, None
