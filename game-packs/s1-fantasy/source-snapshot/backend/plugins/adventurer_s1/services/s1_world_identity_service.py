"""Frozen identity indexes prevent temporary copies of persistent world entities."""

import json


def project_world_identity_guards(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    actors = []
    actor_rows = source.get('actors')
    for row in actor_rows if isinstance(actor_rows, list) else []:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0:
            continue
        name = row.get('canonical_name')
        if not isinstance(name, str) or not name.strip():
            continue
        aliases = row.get('aliases')
        actors.append({
            'id': row['id'], 'canonical_name': name.strip(),
            'aliases': [
                alias.strip() for alias in (aliases if isinstance(aliases, list) else [])
                if isinstance(alias, str) and alias.strip()
            ],
        })
    locations = []
    location_rows = source.get('locations')
    for row in location_rows if isinstance(location_rows, list) else []:
        if not isinstance(row, dict) or type(row.get('location_id')) is not int or row['location_id'] <= 0:
            continue
        name = row.get('location_name')
        if isinstance(name, str) and name.strip():
            locations.append({'location_id': row['location_id'], 'location_name': name.strip()})
    return {'actors': actors, 'locations': locations}


def render_world_identity_guards(
    value: object,
    *,
    actor_ids: set[int] | None = None,
    location_ids: set[int] | None = None,
) -> str:
    """Render an explicit scene projection without changing frozen guards.

    None retains the complete index for existing non-generation callers. An
    empty scope emits no rows; the full frozen index remains the validator's
    authority regardless of what the model sees.
    """
    guards = project_world_identity_guards(value)
    if actor_ids is not None:
        actor_scope = {value for value in actor_ids if type(value) is int and value > 0}
        guards['actors'] = [row for row in guards['actors'] if row['id'] in actor_scope]
    if location_ids is not None:
        location_scope = {value for value in location_ids if type(value) is int and value > 0}
        guards['locations'] = [
            row for row in guards['locations'] if row['location_id'] in location_scope
        ]
    if not guards['actors'] and not guards['locations']:
        return ''
    return (
        '【已登记身份索引·不授予出场或写入权限】\n'
        '索引只说明这些名称属于既有主体与地点，不说明本场共址、可用能力、角色知情或当前阶段。'
        '使用同一主体须沿本轮已授权的精确身份、能力版本与公共线组合；没有该组合时不能把它作为无身份的临时主体重新生成。'
        '同一地点只从本轮精确地点快照继承现状；新探索域不沿用索引地点的身份或既有阶段，'
        '跨地点接触仍须本轮已有的移动或传播依据。未列出的名称也不代表尚未登记。\n'
        + json.dumps(guards, ensure_ascii=False, separators=(',', ':'))
    )


def exact_guarded_actor_ids(subject: object, value: object) -> set[int]:
    """Resolve only complete canonical names or registered aliases, never substrings."""
    if not isinstance(subject, str) or not subject.strip():
        return set()
    name = subject.strip().casefold()
    return {
        row['id'] for row in project_world_identity_guards(value)['actors']
        if name in {item.casefold() for item in [row['canonical_name'], *row['aliases']]}
    }
