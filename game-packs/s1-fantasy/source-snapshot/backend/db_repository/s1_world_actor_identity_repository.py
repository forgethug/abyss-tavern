"""Actor identity reads independent of narrative and lifetime storage budgets."""
from flask import current_app

from plugins.adventurer_s1.models import AdventurerS1WorldActor as Actor


def iter_actor_identities():
    """Keyset pages of identity columns only; never hydrate all actor histories."""
    batch_size = max(1, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_ACTOR_IDENTITY_BATCH_SIZE', 200)))
    last_id = 0
    while True:
        rows = (Actor.query.with_entities(Actor.id, Actor.canonical_name, Actor.aliases)
                .filter(Actor.id > last_id).order_by(Actor.id).limit(batch_size).all())
        if not rows:
            return
        yield from rows
        last_id = rows[-1].id
        if len(rows) < batch_size:
            return


def identity_names(row):
    return [name.strip() for name in (row.canonical_name, *(row.aliases or []))
            if isinstance(name, str) and name.strip()]


def actor_name_exists(name: str) -> bool:
    """Exact names/aliases only. A collision never authorizes updating that actor."""
    return any(name in identity_names(row) for row in iter_actor_identities())


def referenced_actor_ids(texts: list[str]) -> set[int]:
    """Preserve existing exact-name mention protection beyond the old 120 rows."""
    return {row.id for row in iter_actor_identities()
            if any(name in text for name in identity_names(row) for text in texts)}


def prompt_name_registry(*, priority_actor_ids=()) -> list[dict]:
    """Bounded naming hints, not the authoritative identity catalogue."""
    limit = max(1, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_ACTOR_NAME_PROMPT_LIMIT', 120)))
    budget = max(0, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_ACTOR_NAME_PROMPT_CHARS', 12000)))
    import json
    from sqlalchemy import case

    priority_ids = list(dict.fromkeys(priority_actor_ids))
    query = Actor.query.with_entities(Actor.id, Actor.canonical_name, Actor.aliases)
    if priority_ids:
        query = query.order_by(case((Actor.id.in_(priority_ids), 0), else_=1))
    rows = query.order_by(Actor.id.desc()).limit(limit).all()
    result = []
    used = 2
    for row in rows:
        entry = {'canonical_name': row.canonical_name, 'aliases': list(row.aliases or [])}
        size = len(json.dumps(entry, ensure_ascii=False)) + (2 if result else 0)
        if used + size > budget:
            continue
        result.append(entry)
        used += size
    return result
