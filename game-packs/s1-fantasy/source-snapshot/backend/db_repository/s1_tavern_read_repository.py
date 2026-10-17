"""Isolated, bounded ORM reads for tavern cards."""

from contextlib import contextmanager

from sqlalchemy.orm import Session

from db_repository import db_manager


def load_card_unequipped_traits(session, trait_ids):
    """Read descriptions only for choices whose visibility needs verification.

    The caller supplies IDs from cards in this same read-only snapshot. Scalar
    rows avoid loading the trait's owner or populating deferred ORM attributes.
    """
    if not trait_ids:
        return []
    from plugins.adventurer_s1.models import AdventurerS1Trait

    return session.query(
        AdventurerS1Trait.id,
        AdventurerS1Trait.description,
        AdventurerS1Trait.is_archived,
        AdventurerS1Trait.equipped,
    ).filter(
        AdventurerS1Trait.id.in_(trait_ids),
        AdventurerS1Trait.is_archived.is_(False),
        AdventurerS1Trait.equipped.is_(False),
    ).all()


@contextmanager
def tavern_card_read_session():
    """Detach loaded cards and return the connection before serialization.

    The request's scoped Session is deliberately untouched: callers may already
    have loaded authentication state or have unrelated pending work.
    """
    with db_manager.readonly_snapshot() as connection:
        with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
            yield session
