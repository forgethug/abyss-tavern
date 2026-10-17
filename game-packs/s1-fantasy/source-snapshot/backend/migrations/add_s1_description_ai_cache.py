"""Create the content-addressed S1 AI description cache."""

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache


def migrate() -> None:
    AdventurerS1DescriptionAiCache.__table__.create(bind=db.engine, checkfirst=True)


if __name__ == '__main__':
    migrate()
