"""Create the internal carried-asset backlog without scanning player assets."""
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1DescriptionAiBacklog, AdventurerS1DescriptionAiAssetVersion,
)


def migrate() -> None:
    AdventurerS1DescriptionAiBacklog.__table__.create(bind=db.engine, checkfirst=True)
    AdventurerS1DescriptionAiAssetVersion.__table__.create(bind=db.engine, checkfirst=True)
