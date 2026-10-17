"""Bounded recent-activity reads across a player's adventurers."""

from sqlalchemy import select, union_all


def recent_activity_query(query, model, adventurer_ids, *, limit, before=None):
    """Keep filters/joins before each limit and fetch all owners in one SQL.

    Every row in the global first N is in its owner's first N. Merge only those
    scalar candidates, then load the winning ORM rows and their eager relations.
    A global IN + ORDER BY can otherwise scan unrelated players' recent logs.
    """
    ids = sorted(set(adventurer_ids))
    if before is not None:
        query = query.filter(model.created_at < before)
    ordering = (model.created_at.desc(), model.id.desc())
    if len(ids) <= 1:
        return query.filter(model.adventurer_id.in_(ids)).order_by(*ordering).limit(limit)
    pages = []
    for adventurer_id in ids:
        page = (
            query.enable_eagerloads(False).with_entities(model.id, model.created_at)
            .filter(model.adventurer_id == adventurer_id)
            .order_by(*ordering).limit(limit).subquery()
        )
        pages.append(select(page.c.id, page.c.created_at))
    candidates = union_all(*pages).subquery()
    winners = (
        select(candidates.c.id)
        .order_by(candidates.c.created_at.desc(), candidates.c.id.desc()).limit(limit)
    )
    return query.filter(model.id.in_(winners)).order_by(*ordering).limit(limit)
