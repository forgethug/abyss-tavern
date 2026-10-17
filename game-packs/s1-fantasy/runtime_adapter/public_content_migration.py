"""Retire legacy personal defaults without deleting rooms, residents or history.

These identifiers are compatibility keys for existing databases, not privileges.
Runs during the host's database initialization, never on a player GET.
"""


def migrate():
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1DormRoom, AdventurerS1Title

    with db_manager.transaction():
        rooms = AdventurerS1DormRoom.query.filter_by(
            room_key='tavern_owner_room',
        ).with_for_update().all()
        for room in rooms:
            room.room_key = None
            # Preserve capacity, occupants, ownership and custom names.
            if room.name == '酒馆老板的房间':
                room.name = f'单人间 {room.id}'
        titles = AdventurerS1Title.query.filter(
            AdventurerS1Title.is_system.is_(True),
            AdventurerS1Title.code.in_(('pillow_developer', 'kaoyu')),
        ).with_for_update().all()
        for title in titles:
            title.is_enabled = False
            title.frame_key = 'standard'
