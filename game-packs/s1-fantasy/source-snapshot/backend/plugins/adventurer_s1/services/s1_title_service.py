"""S1 冒险者称号、资格与边框配置。"""

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import re
from typing import Optional, Tuple

from sqlalchemy import exists, func, or_
from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1FateJourney,
    AdventurerS1FateCompanionCredit,
    AdventurerS1Title,
    AdventurerS1TitleGrant,
)

from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_HIDDEN_RELIC_TITLE_CODE,
    get_s1_hidden_relic_title_ingredients,
)


FATE_TITLE_CODE = 'fate_engraver'
FATE_COMPANION_TITLE_CODE = 'fate_lantern_bearer'
VALID_FRAME_KEYS = ('standard', 'fate', 'fate_companion', 'hidden_relic')
PREVIEWABLE_TITLE_CODES = (FATE_TITLE_CODE, FATE_COMPANION_TITLE_CODE, S1_HIDDEN_RELIC_TITLE_CODE)
_CODE_PATTERN = re.compile(r'^[a-z][a-z0-9_]{2,49}$')

BUILTIN_TITLES = ({'code': S1_HIDDEN_RELIC_TITLE_CODE, 'name': '万境拾遗者', 'description': '把无人知晓的远方，带回酒馆的灯下。', 'frame_key': 'hidden_relic'}, {'code': FATE_TITLE_CODE, 'name': '命途铭刻者', 'description': '走过最终试炼，把自己的道路刻进命途。', 'frame_key': 'fate'}, {'code': FATE_COMPANION_TITLE_CODE, 'name': '命途守望者', 'description': '四十次陪伴同伴跨越命途关隘，见证每一次关键转折。', 'frame_key': 'fate_companion'})

_LEGACY_FATE_COMPANION_NAME = '他途执灯者'
_LEGACY_FATE_COMPANION_DESCRIPTIONS = (
    '三十次踏入他人的命途转折，在岔路前为同伴执灯。',
    '三十次陪伴同伴跨越命途关隘，见证每一次关键转折。',
)


def seed_s1_titles() -> int:
    """幂等补齐系统称号，并只迁移仍为旧内置文案的陪同称号。"""
    codes = [row['code'] for row in BUILTIN_TITLES]
    existing = {
        row.code: row for row in AdventurerS1Title.query.filter(
            AdventurerS1Title.code.in_(codes),
        ).all()
    }
    additions = [
        AdventurerS1Title(**row, is_system=True, is_enabled=True)
        for row in BUILTIN_TITLES
        if row['code'] not in existing
    ]
    metadata_changed = False
    companion_title = existing.get(FATE_COMPANION_TITLE_CODE)
    if companion_title is not None:
        companion_defaults = next(
            row for row in BUILTIN_TITLES if row['code'] == FATE_COMPANION_TITLE_CODE
        )
        if companion_title.name == _LEGACY_FATE_COMPANION_NAME:
            companion_title.name = companion_defaults['name']
            metadata_changed = True
        if companion_title.description in _LEGACY_FATE_COMPANION_DESCRIPTIONS:
            companion_title.description = companion_defaults['description']
            metadata_changed = True
    if additions:
        ok, err = db_manager.add_all(additions, flush=False)
        if not ok:
            raise RuntimeError(err or '补齐 S1 称号失败')
    if additions or metadata_changed:
        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(err or '提交 S1 称号失败')
    return len(additions)


class S1TitleService:
    """称号目录、自动资格、装备状态和管理员授予的统一服务。"""

    @staticmethod
    def _fate_companion_required() -> int:
        return max(1, int(current_app.config.get(
            'ADVENTURER_S1_FATE_COMPANION_TITLE_REQUIRED', 40,
        )))

    @staticmethod
    def _after_change(adventurer_id: Optional[int] = None) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_adventurer_detail_cache,
            invalidate_dormitory_cache,
        )
        from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
            invalidate_matchmaking_read_cache,
        )
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import (
            invalidate_parties_board_cache,
        )
        invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_dormitory_cache()
        invalidate_parties_board_cache()
        invalidate_matchmaking_read_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            payload = {
                'action': 'title_changed' if adventurer_id is not None else 'title_catalog_changed',
            }
            if adventurer_id is not None:
                payload['ids'] = [adventurer_id]
            socketio.emit('adventurer_changed', payload, namespace='/')
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _automatic_codes(adventurer: AdventurerS1) -> set[str]:
        codes: set[str] = set()
        fate_completed = AdventurerS1FateJourney.query.with_entities(
            AdventurerS1FateJourney.id,
        ).filter(
            AdventurerS1FateJourney.adventurer_id == adventurer.id,
            AdventurerS1FateJourney.breakthrough_completed.is_(True),
            AdventurerS1FateJourney.status == 'completed',
        ).first() is not None
        if fate_completed:
            codes.add(FATE_TITLE_CODE)
        companion_count = AdventurerS1FateCompanionCredit.query.filter_by(
            companion_adventurer_id=adventurer.id,
        ).limit(S1TitleService._fate_companion_required()).count()
        if companion_count >= S1TitleService._fate_companion_required():
            codes.add(FATE_COMPANION_TITLE_CODE)
        return codes

    @staticmethod
    def _manual_title_ids(adventurer_id: int) -> set[int]:
        return {
            int(row[0]) for row in AdventurerS1TitleGrant.query.with_entities(
                AdventurerS1TitleGrant.title_id,
            ).filter(
                AdventurerS1TitleGrant.adventurer_id == adventurer_id,
                AdventurerS1TitleGrant.granted_by_user_id.isnot(None),
            ).all()
        }

    @classmethod
    def _player_title_rows(cls, adventurer: AdventurerS1) -> list[tuple]:
        manual_grant_exists = exists().where(
            AdventurerS1TitleGrant.adventurer_id == adventurer.id,
            AdventurerS1TitleGrant.title_id == AdventurerS1Title.id,
        )
        fate_complete_exists = exists().where(
            AdventurerS1FateJourney.adventurer_id == adventurer.id,
            AdventurerS1FateJourney.breakthrough_completed.is_(True),
            AdventurerS1FateJourney.status == 'completed',
        )
        companion_credit_count = (
            AdventurerS1FateCompanionCredit.query
            .with_entities(func.count(AdventurerS1FateCompanionCredit.id))
            .filter(
                AdventurerS1FateCompanionCredit.companion_adventurer_id == adventurer.id,
            )
            .scalar_subquery()
        )
        available_expression = or_(manual_grant_exists, (AdventurerS1Title.code == FATE_TITLE_CODE) & fate_complete_exists, (AdventurerS1Title.code == FATE_COMPANION_TITLE_CODE) & (companion_credit_count >= cls._fate_companion_required()))
        return (
            AdventurerS1Title.query
            .with_entities(
                AdventurerS1Title,
                manual_grant_exists.label('manual_granted'),
                fate_complete_exists.label('fate_completed'),
                companion_credit_count.label('companion_credit_count'),
            )
            .filter(
                AdventurerS1Title.is_enabled.is_(True),
                or_(
                    available_expression,
                    AdventurerS1Title.code.in_(PREVIEWABLE_TITLE_CODES),
                ),
            )
            .order_by(AdventurerS1Title.is_system.desc(), AdventurerS1Title.id.asc())
            .all()
        )

    @classmethod
    def _row_is_available(
        cls,
        adventurer: AdventurerS1,
        row: tuple,
        *,
        required_companion_count: Optional[int] = None,
    ) -> bool:
        title, manual_granted, fate_completed, companion_credit_count = row
        companion_required = (
            required_companion_count
            if required_companion_count is not None
            else cls._fate_companion_required()
        )
        companion_count = int(companion_credit_count or 0)
        automatic = title.code == FATE_TITLE_CODE and bool(fate_completed) or (title.code == FATE_COMPANION_TITLE_CODE and companion_count >= companion_required)
        return bool(manual_granted) or automatic

    @classmethod
    def _available_title_rows(cls, adventurer: AdventurerS1) -> list[AdventurerS1Title]:
        return [
            row[0]
            for row in cls._player_title_rows(adventurer)
            if cls._row_is_available(adventurer, row)
        ]

    @classmethod
    def player_state(cls, adventurer: AdventurerS1) -> dict:
        rows = cls._player_title_rows(adventurer)
        available = []
        previews = []
        required_companion_count = cls._fate_companion_required()
        for title, manual_granted, fate_completed, companion_credit_count in rows:
            companion_count = int(companion_credit_count or 0)
            is_available = cls._row_is_available(
                adventurer,
                (title, manual_granted, fate_completed, companion_credit_count),
                required_companion_count=required_companion_count,
            )
            if is_available:
                available.append(title)
            if title.code == FATE_TITLE_CODE:
                previews.append({
                    **title.to_public_dict(),
                    'condition': '完成该冒险者的命途旅程与最终突破',
                    'progress_current': 1 if fate_completed else 0,
                    'progress_required': 1,
                    'condition_completed': bool(fate_completed),
                    'is_available': is_available,
                })
            elif title.code == FATE_COMPANION_TITLE_CODE:
                previews.append({
                    **title.to_public_dict(),
                    'condition': f'陪同其他冒险者推进 {required_companion_count} 次命途关键节点',
                    'progress_current': companion_count,
                    'progress_required': required_companion_count,
                    'condition_completed': companion_count >= required_companion_count,
                    'is_available': is_available,
                })
            elif title.code == S1_HIDDEN_RELIC_TITLE_CODE:
                previews.append({
                    **title.to_public_dict(),
                    'condition': '向秘境收藏馆提交十一种藏品，兑换称号与十一印秘匣名片装饰。材料可交易，兑换后绑定当前冒险者。',
                    'progress_current': 1 if is_available else 0,
                    'progress_required': 1,
                    'condition_completed': is_available,
                    'is_available': is_available,
                    'exchange_ingredients': [
                        {
                            'item_name': ingredient['item_name'],
                            'rarity': ingredient['rarity'],
                            'required': ingredient['quantity'],
                        }
                        for ingredient in get_s1_hidden_relic_title_ingredients()
                    ],
                })
        available_ids = {row.id for row in available}
        active = (
            adventurer.active_title_config
            if adventurer.active_title_id in available_ids else None
        )
        return {
            'active_title': active.to_public_dict() if active else None,
            'available_titles': [row.to_public_dict() for row in available],
            'title_previews': previews,
        }

    @classmethod
    def equip(
        cls, adventurer_id: int, user_id: int, title_id: Optional[int],
    ) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with db_manager.transaction():
                adventurer = (
                    AdventurerS1.query.filter_by(id=adventurer_id)
                    .with_for_update().first()
                )
                if not adventurer:
                    return None, '冒险者不存在'
                if int(adventurer.owner_id or 0) != int(user_id):
                    return None, '只能更换自己冒险者的称号'

                if title_id is None:
                    adventurer.active_title_id = None
                else:
                    try:
                        normalized_title_id = int(title_id)
                    except (TypeError, ValueError):
                        return None, 'title_id 必须为整数或 null'
                    available = cls._available_title_rows(adventurer)
                    if normalized_title_id not in {row.id for row in available}:
                        return None, '尚未获得该称号'
                    adventurer.active_title_id = normalized_title_id
        except Exception as exc:  # noqa: BLE001
            return None, player_operation_failure(exc, '更换称号失败')

        cls._after_change(adventurer_id)
        return cls.player_state(adventurer), None

    @staticmethod
    def _validate_title_fields(payload: dict, *, creating: bool) -> Tuple[Optional[dict], Optional[str]]:
        values = {}
        if creating or 'code' in payload:
            code = str(payload.get('code') or '').strip().lower()
            if not _CODE_PATTERN.fullmatch(code):
                return None, 'code 仅支持 3～50 位小写字母、数字和下划线，且以字母开头'
            values['code'] = code
        if creating or 'name' in payload:
            name = str(payload.get('name') or '').strip()
            if not name or len(name) > 50:
                return None, '称号名称长度须为 1～50 字'
            values['name'] = name
        if creating or 'description' in payload:
            description = str(payload.get('description') or '').strip()
            if len(description) > 200:
                return None, '称号描述不能超过 200 字'
            values['description'] = description
        if creating or 'frame_key' in payload:
            frame_key = str(payload.get('frame_key') or 'standard').strip()
            if frame_key not in VALID_FRAME_KEYS:
                return None, '边框样式无效'
            values['frame_key'] = frame_key
        if 'is_enabled' in payload:
            if not isinstance(payload.get('is_enabled'), bool):
                return None, 'is_enabled 必须为布尔值'
            values['is_enabled'] = payload['is_enabled']
        return values, None

    @classmethod
    def admin_list(cls) -> dict:
        titles = AdventurerS1Title.query.order_by(AdventurerS1Title.id.asc()).all()
        grant_counts = (
            AdventurerS1TitleGrant.query.with_entities(
                AdventurerS1TitleGrant.title_id,
                func.count(AdventurerS1TitleGrant.id).filter(
                    AdventurerS1TitleGrant.granted_by_user_id.isnot(None),
                ),
                func.count(AdventurerS1TitleGrant.id).filter(
                    AdventurerS1TitleGrant.granted_by_user_id.is_(None),
                ),
            ).group_by(AdventurerS1TitleGrant.title_id).all()
        )
        manual_counts = {title_id: manual for title_id, manual, _ in grant_counts}
        exchange_counts = {title_id: exchanged for title_id, _, exchanged in grant_counts}
        fate_count = AdventurerS1FateJourney.query.filter(
            AdventurerS1FateJourney.breakthrough_completed.is_(True),
            AdventurerS1FateJourney.status == 'completed',
        ).count()
        companion_count = (
            AdventurerS1FateCompanionCredit.query
            .with_entities(AdventurerS1FateCompanionCredit.companion_adventurer_id)
            .group_by(AdventurerS1FateCompanionCredit.companion_adventurer_id)
            .having(func.count(AdventurerS1FateCompanionCredit.id) >= cls._fate_companion_required())
            .count()
        )
        rows = []
        for title in titles:
            row = title.to_admin_dict()
            row['manual_grant_count'] = int(manual_counts.get(title.id, 0) or 0)
            row['exchange_grant_count'] = int(exchange_counts.get(title.id, 0) or 0)
            automatic_counts = {FATE_TITLE_CODE: fate_count, FATE_COMPANION_TITLE_CODE: companion_count}
            row['automatic_holder_count'] = automatic_counts.get(title.code, 0)
            rows.append(row)
        return {'titles': rows, 'frame_options': list(VALID_FRAME_KEYS)}

    @classmethod
    def admin_create(cls, payload: dict) -> Tuple[Optional[dict], Optional[str]]:
        values, err = cls._validate_title_fields(payload, creating=True)
        if err:
            return None, err
        if AdventurerS1Title.query.filter(
            (AdventurerS1Title.code == values['code']) |
            (AdventurerS1Title.name == values['name'])
        ).first():
            return None, '称号 code 或名称已存在'
        title = AdventurerS1Title(**values, is_system=False)
        ok, err = db_manager.add(title)
        if not ok:
            return None, err
        ok, err = db_manager.commit()
        if not ok:
            return None, err
        return title.to_admin_dict(), None

    @classmethod
    def admin_update(cls, title_id: int, payload: dict) -> Tuple[Optional[dict], Optional[str]]:
        title = AdventurerS1Title.query.filter_by(id=title_id).with_for_update().first()
        if not title:
            return None, '称号不存在'
        values, err = cls._validate_title_fields(payload, creating=False)
        if err:
            return None, err
        if title.is_system:
            values.pop('code', None)
        duplicate = AdventurerS1Title.query.filter(
            AdventurerS1Title.id != title.id,
            ((AdventurerS1Title.code == values.get('code')) if values.get('code') else False) |
            ((AdventurerS1Title.name == values.get('name')) if values.get('name') else False),
        ).first()
        if duplicate:
            return None, '称号 code 或名称已存在'
        for key, value in values.items():
            setattr(title, key, value)
        if values.get('is_enabled') is False:
            AdventurerS1.query.filter_by(active_title_id=title.id).update(
                {AdventurerS1.active_title_id: None}, synchronize_session=False,
            )
        ok, err = db_manager.commit()
        if not ok:
            return None, err
        cls._after_change()
        return title.to_admin_dict(), None

    @staticmethod
    def admin_delete(title_id: int) -> Optional[str]:
        title = AdventurerS1Title.query.filter_by(id=title_id).first()
        if not title:
            return '称号不存在'
        if title.is_system:
            return '系统称号不能删除，可以停用'
        ok, err = db_manager.delete_instance(title)
        if not ok:
            return err
        ok, err = db_manager.commit()
        if not ok:
            return err
        S1TitleService._after_change()
        return None

    @classmethod
    def admin_adventurer_state(cls, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        adventurer = AdventurerS1.query.filter_by(id=adventurer_id).first()
        if not adventurer:
            return None, '冒险者不存在'
        manual_ids = cls._manual_title_ids(adventurer_id)
        exchange_ids = {
            row[0] for row in AdventurerS1TitleGrant.query.with_entities(
                AdventurerS1TitleGrant.title_id,
            ).filter(
                AdventurerS1TitleGrant.adventurer_id == adventurer_id,
                AdventurerS1TitleGrant.granted_by_user_id.is_(None),
            ).all()
        }
        automatic_codes = cls._automatic_codes(adventurer)
        return {
            'adventurer': {'id': adventurer.id, 'name': adventurer.name, 'owner_id': adventurer.owner_id},
            'active_title': (
                adventurer.active_title_config.to_public_dict()
                if adventurer.active_title_config and adventurer.active_title_config.is_enabled else None
            ),
            'manual_title_ids': sorted(manual_ids),
            'exchange_title_ids': sorted(exchange_ids),
            'automatic_title_codes': sorted(automatic_codes),
            'titles': [row.to_admin_dict() for row in AdventurerS1Title.query.order_by(AdventurerS1Title.id).all()],
        }, None

    @classmethod
    def admin_grant(
        cls, adventurer_id: int, title_id: int, admin_user_id: int, note: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with db_manager.transaction():
                adventurer = AdventurerS1.query.filter_by(
                    id=adventurer_id,
                ).with_for_update().first()
                title = AdventurerS1Title.query.filter_by(id=title_id).first()
                if not adventurer:
                    return None, '冒险者不存在'
                if not title:
                    return None, '称号不存在'
                if not title.is_enabled:
                    return None, '称号已停用'
                if title.code in cls._automatic_codes(adventurer):
                    return cls.admin_adventurer_state(adventurer_id)[0], None
                if AdventurerS1TitleGrant.query.filter_by(
                    adventurer_id=adventurer_id, title_id=title_id,
                ).first() is None:
                    grant = AdventurerS1TitleGrant(
                        adventurer_id=adventurer_id,
                        title_id=title_id,
                        granted_by_user_id=admin_user_id,
                        note=str(note or '').strip()[:200],
                    )
                    ok, err = db_manager.add(grant)
                    if not ok:
                        raise RuntimeError(err or '授予称号失败')
        except Exception as exc:  # noqa: BLE001
            return None, f'授予称号失败：{exc}'
        cls._after_change(adventurer_id)
        return cls.admin_adventurer_state(adventurer_id)

    @classmethod
    def admin_revoke(cls, adventurer_id: int, title_id: int) -> Tuple[Optional[dict], Optional[str]]:
        try:
            with db_manager.transaction():
                adventurer = AdventurerS1.query.filter_by(
                    id=adventurer_id,
                ).with_for_update().first()
                if not adventurer:
                    return None, '冒险者不存在'
                grant = AdventurerS1TitleGrant.query.filter_by(
                    adventurer_id=adventurer_id, title_id=title_id,
                ).first()
                if grant:
                    ok, err = db_manager.delete_instance(grant)
                    if not ok:
                        raise RuntimeError(err or '撤销称号失败')
                title = AdventurerS1Title.query.filter_by(id=title_id).first()
                remains_automatic = bool(
                    title and title.code in cls._automatic_codes(adventurer)
                )
                if adventurer.active_title_id == title_id and not remains_automatic:
                    adventurer.active_title_id = None
        except Exception as exc:  # noqa: BLE001
            return None, f'撤销称号失败：{exc}'
        cls._after_change(adventurer_id)
        return cls.admin_adventurer_state(adventurer_id)
