"""Independent catalog reads for S1 validation without business writes."""

from sqlalchemy import select

from extensions import db


def read_item_use_condition_rows(item_names: set[str]) -> list[tuple]:
    """Return committed scalar fields without opening a scoped Session.

    This boundary is for pure validation. Callers that need their own pending
    catalog changes must retain their existing transaction/session read path.
    """
    if not item_names:
        return []
    from plugins.adventurer_s1.models import AdventurerS1Item as Item

    statement = select(Item.name, Item.use_condition).where(
        Item.name.in_(list(item_names)),
    )
    with db.engine.connect() as connection:
        connection = connection.execution_options(isolation_level='AUTOCOMMIT')
        rows = connection.execute(statement).all()
    return [(name, condition) for name, condition in rows]
