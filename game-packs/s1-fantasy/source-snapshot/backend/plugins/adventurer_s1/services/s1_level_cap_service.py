"""S1 level-cap maintenance helpers."""

from __future__ import annotations

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1


def _current_max_level() -> int:
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    try:
        return max(1, int(S1SettingsService.get('adventurer_max_level') or 10))
    except (TypeError, ValueError):
        return 10


class S1LevelCapService:
    """Maintenance operations for exp stored at the current level cap."""

    @staticmethod
    def preview_cap_exp_reset() -> tuple[dict, str | None]:
        max_level = _current_max_level()
        rows = (
            AdventurerS1.query
            .with_entities(AdventurerS1.level, AdventurerS1.exp)
            .filter(AdventurerS1.level >= max_level, AdventurerS1.exp > 0)
            .all()
        )
        total_exp = sum(int(exp or 0) for _level, exp in rows)
        return {
            'max_level': max_level,
            'affected_count': len(rows),
            'total_exp_to_clear': total_exp,
            'max_exp': max((int(exp or 0) for _level, exp in rows), default=0),
        }, None

    @staticmethod
    def reset_cap_exp(*, admin_user_id: int | None = None) -> tuple[dict, str | None]:
        max_level = _current_max_level()
        with db_manager.transaction():
            rows = (
                AdventurerS1.query
                .with_entities(AdventurerS1.id, AdventurerS1.exp)
                .filter(AdventurerS1.level >= max_level, AdventurerS1.exp > 0)
                .all()
            )
            affected_ids = [int(row_id) for row_id, _exp in rows]
            total_exp = sum(int(exp or 0) for _row_id, exp in rows)
            if affected_ids:
                AdventurerS1.query.filter(AdventurerS1.id.in_(affected_ids)).update(
                    {AdventurerS1.exp: 0},
                    synchronize_session=False,
                )
        return {
            'max_level': max_level,
            'affected_count': len(affected_ids),
            'total_exp_cleared': total_exp,
            'admin_user_id': admin_user_id,
        }, None

    @staticmethod
    def open_level_cap_to_15(*, admin_user_id: int | None = None) -> tuple[dict, str | None]:
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

        old_max_level = _current_max_level()
        ok, err = S1SettingsService.set('adventurer_max_level', 15, updated_by_user_id=admin_user_id)
        if not ok:
            return {}, err
        current_app.config['ADVENTURER_S1_MAX_LEVEL'] = 15
        return {
            'old_max_level': old_max_level,
            'max_level': 15,
            'admin_user_id': admin_user_id,
        }, None
