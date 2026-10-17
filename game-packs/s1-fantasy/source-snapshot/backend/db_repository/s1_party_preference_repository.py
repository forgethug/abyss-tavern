"""S1 玩家匹配偏好的批量持久化；事务由调用服务管理。"""

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1


class S1PartyPreferenceRepository:
    @staticmethod
    def get_owned_preferences(adventurer_ids: list[int], owner_id: int, realm_code: str) -> dict:
        """只读取缓存名片中本人的偏好，不加载完整冒险者关系。"""
        if not adventurer_ids:
            return {}
        return dict(db_manager.query(AdventurerS1.id, AdventurerS1.party_preference).filter(
            AdventurerS1.id.in_(adventurer_ids),
            AdventurerS1.owner_id == owner_id,
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.realm_code == realm_code,
        ).all())

    @staticmethod
    def switch_all_player_auto_to_manual(realm_code: str) -> int:
        """一次 UPDATE 覆盖本区服全部在册玩家，不加载玩家列表。"""
        return db_manager.query(AdventurerS1).filter(
            AdventurerS1.realm_code == realm_code,
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.owner_id.isnot(None),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            AdventurerS1.party_preference == 'auto',
        ).update({
            AdventurerS1.party_preference: 'manual',
        }, synchronize_session=False)
