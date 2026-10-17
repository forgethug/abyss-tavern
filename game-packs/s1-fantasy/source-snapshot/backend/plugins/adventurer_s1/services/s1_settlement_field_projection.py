"""Lossless settlement field projection shared by generation and repair consumers.

Keep invalid containers/values for the owning validator. Never truncate records,
invent missing evidence, or change asset identity, quantity or recovery state.
"""
from copy import deepcopy

from utils.ai_field_names import normalize_ai_description_field


def project_s1_description_aliases(
    value,
    *,
    description_key='desc',
    alias_keys=('description',),
):
    """Drop only exact copies of an existing non-empty description field."""
    if not isinstance(value, dict):
        return value
    projected = dict(value)
    description = projected.get(description_key)
    if isinstance(description, str) and description:
        for key in alias_keys:
            if key != description_key and projected.get(key) == description:
                projected.pop(key, None)
    return projected


def project_s1_member_description_aliases(value):
    """Project description aliases without normalizing historical evidence."""
    if not isinstance(value, dict):
        return value
    projected = dict(value)
    for field in ('new_skills', 'new_traits', 'new_equipments'):
        rows = projected.get(field)
        if isinstance(rows, list):
            projected[field] = [project_s1_description_aliases(row) for row in rows]
        elif isinstance(rows, dict):
            projected[field] = {
                name: project_s1_description_aliases(row) for name, row in rows.items()
            }

    def project_thread(thread):
        return project_s1_description_aliases(
            project_s1_description_aliases(thread),
            description_key='summary',
            alias_keys=('desc', 'description'),
        )

    threads = projected.get('story_threads')
    if isinstance(threads, list):
        projected['story_threads'] = [project_thread(row) for row in threads]
    elif isinstance(threads, dict):
        projected['story_threads'] = project_thread(threads)
    return projected


def _record(value, keys):
    if not isinstance(value, dict):
        return deepcopy(value)
    return project_s1_description_aliases({
        key: deepcopy(item) for key, item in value.items() if key in keys
    })


def _records(value, keys):
    return ([_record(row, keys) for row in value]
            if isinstance(value, list) else deepcopy(value))


def project_s1_trait_fields(value):
    projected = normalize_ai_description_field(_record(value, {
        'name', 'desc', 'description', 'lore_append', 'lore', 'witness',
        'asset_dependencies',
    }))
    if isinstance(projected, dict) and 'asset_dependencies' in projected:
        projected['asset_dependencies'] = _records(projected['asset_dependencies'], {
            # Request-local references must survive until frozen-ID resolution.
            'asset_kind', 'asset_ref', 'asset_id', 'new_asset_name',
        })
    return projected


def project_s1_member_settlement_fields(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    projected = dict(value)
    skills = projected.get('new_skills')
    def project_skill(skill):
        skill = _record(skill, {
            'name', 'desc', 'description', 'recovery_minutes',
        })
        return normalize_ai_description_field(skill)
    if isinstance(skills, list):
        projected['new_skills'] = [project_skill(row) for row in skills]
    elif isinstance(skills, dict):
        projected['new_skills'] = {name: project_skill(row) for name, row in skills.items()}
    traits = projected.get('new_traits')
    if isinstance(traits, list):
        projected['new_traits'] = [project_s1_trait_fields(row) for row in traits]
    elif isinstance(traits, dict):
        projected['new_traits'] = {
            name: project_s1_trait_fields(row) for name, row in traits.items()
        }
    equipment = projected.get('new_equipments')
    if isinstance(equipment, dict):
        projected['new_equipments'] = {
            name: normalize_ai_description_field(_record(row, {
                'type', 'rarity', 'desc', 'description', 'quantity', 'uses_left',
                'evolution_stage', 'recovery_minutes', 'damage_stage', 'damage_note',
            })) for name, row in equipment.items()
        }
    for field in ('skill_updates', 'equipment_updates'):
        if isinstance(projected.get(field), list):
            projected[field] = [normalize_ai_description_field(row) for row in projected[field]]
    if 'relationship_changes' in projected:
        projected['relationship_changes'] = _records(projected['relationship_changes'], {
            'target_id', 'delta', 'reason',
        })
    threads = projected.get('story_threads')
    def project_thread(thread):
        thread = _record(thread, {
            'id', 'thread_id', 'predecessor_id', 'title', 'name', 'summary',
            'desc', 'description', 'tags', 'entity_refs', 'status', 'weight',
        })
        thread = project_s1_description_aliases(
            thread,
            description_key='summary',
            alias_keys=('desc', 'description'),
        )
        if isinstance(thread, dict) and 'entity_refs' in thread:
            thread['entity_refs'] = _record(thread['entity_refs'], {
                'world_actor_ids', 'world_story_thread_ids', 'faction_keys',
            })
        return thread
    if isinstance(threads, list):
        projected['story_threads'] = [project_thread(row) for row in threads]
    elif isinstance(threads, dict):
        projected['story_threads'] = project_thread(threads)
    return projected


def project_s1_settlement_fields(value):
    """Retain server receipts while projecting the settlement fields they protect."""
    projected = project_s1_top_settlement_fields(value)
    if isinstance(projected, dict) and isinstance(projected.get('member_updates'), list):
        projected['member_updates'] = [
            project_s1_member_settlement_fields(row) for row in projected['member_updates']
        ]
    return projected


def project_s1_top_settlement_fields(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    projected = dict(value)
    if 'unassigned_items' in projected:
        from .s1_team_loot_contract import project_team_loot
        projected['unassigned_items'] = project_team_loot(projected['unassigned_items'])
    if 'commission_results' in projected:
        projected['commission_results'] = _records(projected['commission_results'], {
            'commission_id', 'completed', 'completed_count', 'count', 'progress_count',
        })
    completion = projected.get('completion_check')
    if isinstance(completion, dict):
        completion = _record(completion, {
            'story_finished', 'core_resolved', 'exit_resolved', 'ending_type',
        })
        projected['completion_check'] = completion
    return projected
