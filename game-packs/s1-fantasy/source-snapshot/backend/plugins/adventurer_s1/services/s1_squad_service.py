"""S1 纹章小队服务。

纹章小队只提供冒险者名片上的静态装饰，不参与探险组队、数值、任务或结算。
"""

import logging
import unicodedata
from types import SimpleNamespace
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, load_only, noload

from db_repository import ConcurrencyError, db_manager
from db_repository.decorators import db_retry
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Squad
from utils.pagination import build_paginated_response

logger = logging.getLogger(__name__)

SQUAD_DISABLED_ERROR = '纹章小队尚未开放'

_EMBLEM_OPTIONS = {
    'frame': {
        'round': '月轮',
        'shield': '古盾',
        'diamond': '棱晶',
        'hex': '六方印',
        'square': '方印',
        'arch': '拱门',
        'crest': '尖徽',
        'octagon': '八方印',
        'banner': '战旗',
        'rune': '秘文环',
        'tablet': '石碑',
        'petal': '花瓣章',
    },
    'pattern': {
        'solid': '纯色',
        'split': '对分',
        'chevron': '山纹',
        'rays': '辉芒',
        'diagonal': '斜裁',
        'quarters': '四象',
        'cross': '十字',
        'stripes': '战纹',
        'rings': '涟漪',
        'checker': '棋格',
        'horizon': '天际',
        'halo': '光环',
    },
    'sigil': {
        'star': '星芒',
        'sword': '古剑',
        'moon': '弦月',
        'flame': '火种',
        'tower': '高塔',
        'leaf': '月桂',
        'shield': '守护盾',
        'sun': '曜日',
        'eye': '灵视',
        'skull': '骸骨',
        'key': '秘钥',
        'paw': '兽爪',
        'rose': '蔷薇',
        'lightning': '雷霆',
        'snow': '霜花',
        'compass': '罗盘',
        'planet': '星环',
        'gem': '宝石',
        'hammer': '战锤',
        'anchor': '远航',
        'chalice': '圣杯',
        'dice': '命骰',
        'book': '秘典',
        'music': '乐章',
        'heart': '心火',
        'water': '浪潮',
        'cloud': '云冠',
        'flower': '花冠',
        'crown': '荣冠',
        'prism': '秘符',
    },
    'primary': {
        'crimson': ('绯红', '#a83a4b'),
        'azure': ('苍蓝', '#3674a5'),
        'emerald': ('翡翠', '#3d7f66'),
        'violet': ('暮紫', '#76558f'),
        'gold': ('鎏金', '#c89a3d'),
        'ivory': ('象牙白', '#e7dfcc'),
        'obsidian': ('黑曜', '#252a31'),
        'copper': ('赤铜', '#a96845'),
        'sapphire': ('深靛', '#294a78'),
        'teal': ('青碧', '#2d7c79'),
        'jade': ('玉青', '#6f9d64'),
        'rose': ('玫瑰', '#b65a78'),
        'amber': ('琥珀', '#cf7d32'),
        'silver': ('秘银', '#aeb6c2'),
        'umber': ('棕褐', '#6a4c3b'),
        'midnight': ('夜蓝', '#2b294f'),
        'coral': ('珊瑚', '#d3685c'),
        'lavender': ('薰紫', '#9a79b6'),
    },
}
_EMBLEM_OPTIONS['secondary'] = _EMBLEM_OPTIONS['primary']
_EMBLEM_OPTIONS['accent'] = _EMBLEM_OPTIONS['primary']
_EMBLEM_FIELDS = ('frame', 'pattern', 'sigil', 'primary', 'secondary', 'accent')
_DEFAULT_EMBLEM = {
    'frame': 'shield',
    'pattern': 'split',
    'sigil': 'star',
    'primary': 'crimson',
    'secondary': 'obsidian',
    'accent': 'gold',
}


class S1SquadService:
    """纯装饰纹章小队的查询与写入入口。"""

    @staticmethod
    def enabled() -> bool:
        return bool(current_app.config.get('ADVENTURER_S1_SQUADS_ENABLED', False))

    @staticmethod
    def _member_slots() -> int:
        return max(
            0,
            int(current_app.config.get('ADVENTURER_S1_SQUAD_MEMBER_SLOTS', 9) or 0),
        )

    @staticmethod
    def _total_capacity() -> int:
        # 配置的是“不含队长的成员槽”，总容量始终额外包含 1 名队长。
        return 1 + S1SquadService._member_slots()

    @staticmethod
    def _name_max_length() -> int:
        return max(
            1,
            int(current_app.config.get('ADVENTURER_S1_SQUAD_NAME_MAX_LENGTH', 12) or 12),
        )

    @staticmethod
    def _create_gold_cost() -> float:
        return round(
            max(
                0.0,
                float(
                    current_app.config.get(
                        'ADVENTURER_S1_SQUAD_CREATE_GOLD_COST',
                        5000.0,
                    ) or 0.0
                ),
            ),
            2,
        )

    @staticmethod
    def feature_config() -> dict:
        colors = [
            {'key': key, 'label': label, 'value': value}
            for key, (label, value) in _EMBLEM_OPTIONS['primary'].items()
        ]
        return {
            'create_gold_cost': S1SquadService._create_gold_cost(),
            'member_slots': S1SquadService._member_slots(),
            'total_capacity': S1SquadService._total_capacity(),
            'name_max_length': S1SquadService._name_max_length(),
            'catalog': {
                'frames': [
                    {'key': key, 'label': label}
                    for key, label in _EMBLEM_OPTIONS['frame'].items()
                ],
                'patterns': [
                    {'key': key, 'label': label}
                    for key, label in _EMBLEM_OPTIONS['pattern'].items()
                ],
                'sigils': [
                    {'key': key, 'label': label}
                    for key, label in _EMBLEM_OPTIONS['sigil'].items()
                ],
                'colors': colors,
                'default_emblem': dict(_DEFAULT_EMBLEM),
            },
        }

    @staticmethod
    def _require_enabled() -> Optional[str]:
        return None if S1SquadService.enabled() else SQUAD_DISABLED_ERROR

    @staticmethod
    def _parse_positive_id(value, label: str) -> Tuple[Optional[int], Optional[str]]:
        if isinstance(value, bool):
            return None, f'{label}无效'
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None, f'{label}无效'
        if parsed <= 0:
            return None, f'{label}无效'
        return parsed, None

    @staticmethod
    def _normalize_name(value) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        if not isinstance(value, str):
            return None, None, '请输入小队名称'
        normalized = unicodedata.normalize('NFKC', value)
        if any(unicodedata.category(char).startswith('C') for char in normalized):
            return None, None, '小队名称不能包含控制字符或换行'
        name = ' '.join(normalized.strip().split())
        if not name:
            return None, None, '请输入小队名称'
        if len(name) > S1SquadService._name_max_length():
            return (
                None,
                None,
                f'小队名称最多 {S1SquadService._name_max_length()} 个字符',
            )
        return name, name.casefold(), None

    @staticmethod
    def _validate_emblem(
        raw,
        *,
        base: Optional[dict] = None,
        require_change: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return None, '纹章配置格式无效'
        unknown = set(raw) - set(_EMBLEM_FIELDS)
        if unknown:
            return None, '纹章只能使用酒馆提供的固定素材'
        if require_change and not raw:
            return None, '请选择要修改的纹章素材'

        result = dict(base or _DEFAULT_EMBLEM)
        for field in _EMBLEM_FIELDS:
            if field not in raw:
                continue
            key = raw.get(field)
            if not isinstance(key, str) or key not in _EMBLEM_OPTIONS[field]:
                return None, f'纹章素材 {field} 无效'
            result[field] = key
        return result, None

    @staticmethod
    def _apply_emblem(squad: AdventurerS1Squad, emblem: dict) -> None:
        squad.emblem_frame = emblem['frame']
        squad.emblem_pattern = emblem['pattern']
        squad.emblem_sigil = emblem['sigil']
        squad.emblem_primary = emblem['primary']
        squad.emblem_secondary = emblem['secondary']
        squad.emblem_accent = emblem['accent']

    @staticmethod
    def _precheck_owned_adventurer(
        adventurer_id,
        user_id: int,
        *,
        require_playable: bool = True,
    ) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        """在扣款锁之前拒绝明显无权请求；事务锁后仍会重复权威校验。"""
        parsed_id, err = S1SquadService._parse_positive_id(adventurer_id, '冒险者')
        if err:
            return None, err
        adv = (
            AdventurerS1.query
            .populate_existing()
            .options(
                load_only(
                    AdventurerS1.id,
                    AdventurerS1.owner_id,
                    AdventurerS1.status,
                    AdventurerS1.is_player_created,
                    AdventurerS1.gold,
                    AdventurerS1.squad_id,
                ),
                noload('*'),
            )
            .filter(AdventurerS1.id == parsed_id)
            .first()
        )
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '无权操作此冒险者'
        if require_playable:
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                AdventurerS1Service,
            )

            err = AdventurerS1Service.playable_status_error(adv.status)
            if err:
                return None, err
        if not adv.is_player_created:
            return None, '只有玩家自建冒险者可以加入纹章小队'
        return adv, None

    @staticmethod
    def _lock_owned_adventurer(
        adventurer_id,
        user_id: int,
        *,
        require_playable: bool = True,
    ) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        parsed_id, err = S1SquadService._parse_positive_id(adventurer_id, '冒险者')
        if err:
            return None, err
        adv = (
            AdventurerS1.query
            .populate_existing()
            .options(
                load_only(
                    AdventurerS1.id,
                    AdventurerS1.owner_id,
                    AdventurerS1.name,
                    AdventurerS1.level,
                    AdventurerS1.character_class,
                    AdventurerS1.status,
                    AdventurerS1.is_player_created,
                    AdventurerS1.squad_id,
                    AdventurerS1.gold,
                ),
                noload('*'),
            )
            .filter(AdventurerS1.id == parsed_id)
            .with_for_update()
            .first()
        )
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '无权操作此冒险者'
        if require_playable:
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                AdventurerS1Service,
            )

            err = AdventurerS1Service.playable_status_error(adv.status)
            if err:
                return None, err
        if not adv.is_player_created:
            return None, '只有玩家自建冒险者可以加入纹章小队'
        return adv, None

    @staticmethod
    def _lock_squad(squad_id) -> Tuple[Optional[AdventurerS1Squad], Optional[str]]:
        parsed_id, err = S1SquadService._parse_positive_id(squad_id, '小队')
        if err:
            return None, err
        squad = (
            AdventurerS1Squad.query
            .populate_existing()
            .filter(AdventurerS1Squad.id == parsed_id)
            .with_for_update()
            .first()
        )
        if not squad:
            return None, '纹章小队不存在'
        return squad, None

    @staticmethod
    def _member_rows(squad_id: int) -> list[AdventurerS1]:
        return (
            AdventurerS1.query
            .options(
                load_only(
                    AdventurerS1.id,
                    AdventurerS1.owner_id,
                    AdventurerS1.name,
                    AdventurerS1.level,
                    AdventurerS1.character_class,
                    AdventurerS1.squad_id,
                ),
                noload('*'),
            )
            .filter(AdventurerS1.squad_id == squad_id)
            .order_by(AdventurerS1.id.asc())
            .all()
        )

    @staticmethod
    def _member_payload(adv: AdventurerS1, leader_id: int) -> dict:
        is_leader = adv.id == leader_id
        return {
            'adventurer_id': adv.id,
            'name': adv.name,
            'level': int(adv.level or 1),
            'character_class': adv.character_class,
            'is_leader': is_leader,
            'role': 'leader' if is_leader else 'member',
        }

    @staticmethod
    def _public_squad_payload(
        squad: AdventurerS1Squad,
        *,
        leader_name: Optional[str],
        member_count: int,
    ) -> dict:
        total_capacity = S1SquadService._total_capacity()
        return {
            **squad.to_public_dict(),
            'leader_adventurer_id': squad.leader_adventurer_id,
            'leader_name': leader_name or '未知冒险者',
            'member_count': int(member_count),
            'member_slots': S1SquadService._member_slots(),
            'total_capacity': total_capacity,
            'is_full': int(member_count) >= total_capacity,
        }

    @staticmethod
    def _squad_detail(
        squad_id: int,
        *,
        current_adventurer_id: Optional[int] = None,
    ) -> Optional[dict]:
        squad = AdventurerS1Squad.query.session.get(AdventurerS1Squad, squad_id)
        if not squad:
            return None
        members = S1SquadService._member_rows(squad.id)
        leader = next(
            (adv for adv in members if adv.id == squad.leader_adventurer_id),
            None,
        )
        payload = S1SquadService._public_squad_payload(
            squad,
            leader_name=leader.name if leader else None,
            member_count=len(members),
        )
        payload.update({
            'is_leader': bool(
                current_adventurer_id
                and int(current_adventurer_id) == squad.leader_adventurer_id
            ),
            'members': [
                S1SquadService._member_payload(adv, squad.leader_adventurer_id)
                for adv in members
            ],
        })
        return payload

    @staticmethod
    def _notify_changed(adventurers: list[AdventurerS1], action: str) -> None:
        try:
            adv_ids = sorted({
                int(adv.id) for adv in adventurers if getattr(adv, 'id', None)
            })
            owner_ids = sorted({
                int(adv.owner_id)
                for adv in adventurers
                if getattr(adv, 'owner_id', None)
            })
            if not adv_ids and not owner_ids:
                return

            from plugins.adventurer_s1.services.adventurer_s1_home_service import (
                invalidate_s1_init_cache,
            )
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                invalidate_adventurer_detail_cache,
                invalidate_my_activity_cache,
            )

            for adv_id in adv_ids:
                invalidate_adventurer_detail_cache(adv_id)
            for owner_id in owner_ids:
                invalidate_s1_init_cache(owner_id)
                invalidate_my_activity_cache(owner_id)

            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio

            socketio.emit(
                'adventurer_changed',
                {'action': action, 'ids': adv_ids},
                namespace='/',
            )
        except Exception:
            # 写事务已提交；缓存与 Socket 通知只能 best effort，绝不能把成功操作
            # 伪装成失败响应。卡片徽章在缓存命中时还会重新从数据库投影兜底。
            logger.warning(
                'S1 纹章小队变更通知失败 action=%s',
                action,
                exc_info=True,
            )

    @staticmethod
    def _changed_refs(adventurers: list[AdventurerS1]) -> list[SimpleNamespace]:
        """在 commit 使 ORM 实例过期前保留失效通知所需的最小身份。"""
        return [
            SimpleNamespace(id=int(adv.id), owner_id=int(adv.owner_id))
            for adv in adventurers
            if adv.id is not None and adv.owner_id is not None
        ]

    @staticmethod
    def attach_badges(items: list[dict], *, session=None) -> list[dict]:
        """批量给名片白名单投影纹章；功能关闭时零查询、零字段。"""
        if not S1SquadService.enabled() or not items:
            return items
        by_id = {
            int(item['id']): item
            for item in items
            if isinstance(item, dict) and item.get('id')
        }
        for item in by_id.values():
            item['squad_badge'] = None
        if not by_id:
            return items

        query = (
            db_manager.query(AdventurerS1.id, AdventurerS1Squad)
            .join(
                AdventurerS1Squad,
                AdventurerS1Squad.id == AdventurerS1.squad_id,
            )
            .filter(AdventurerS1.id.in_(by_id))
        )
        if session is not None:
            query = query.with_session(session)
        rows = query.all()
        for adventurer_id, squad in rows:
            by_id[adventurer_id]['squad_badge'] = {
                'squad_id': squad.id,
                'squad_name': squad.name,
                'emblem': squad.emblem_dict(),
            }
        return items

    @staticmethod
    def list_squads(
        user_id: int,
        page: int,
        per_page: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err

        count_sq = (
            db_manager.query(
                AdventurerS1.squad_id.label('squad_id'),
                func.count(AdventurerS1.id).label('member_count'),
            )
            .filter(AdventurerS1.squad_id.isnot(None))
            .group_by(AdventurerS1.squad_id)
            .subquery()
        )
        leader_adv = aliased(AdventurerS1)
        query = (
            db_manager.query(
                AdventurerS1Squad,
                leader_adv.name,
                func.coalesce(count_sq.c.member_count, 0),
            )
            .outerjoin(
                leader_adv,
                leader_adv.id == AdventurerS1Squad.leader_adventurer_id,
            )
            .outerjoin(count_sq, count_sq.c.squad_id == AdventurerS1Squad.id)
            .order_by(AdventurerS1Squad.created_at.desc())
        )
        total = query.count()
        rows = (
            query
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        items = [
            S1SquadService._public_squad_payload(
                squad,
                leader_name=leader_name,
                member_count=int(member_count or 0),
            )
            for squad, leader_name, member_count in rows
        ]

        my_adv = (
            db_manager.query(AdventurerS1.id, AdventurerS1.squad_id)
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.squad_id.isnot(None),
            )
            .order_by(AdventurerS1.created_at.desc())
            .first()
        )
        my_squad = (
            S1SquadService._squad_detail(
                my_adv.squad_id,
                current_adventurer_id=my_adv.id,
            )
            if my_adv else None
        )
        return {
            'feature': S1SquadService.feature_config(),
            'my_squad': my_squad,
            'squads': build_paginated_response(items, total, page, per_page),
        }, None

    @staticmethod
    def create_squad(
        user_id: int,
        adventurer_id,
        name,
        emblem,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err
        normalized_name, name_key, err = S1SquadService._normalize_name(name)
        if err:
            return None, err
        normalized_emblem, err = S1SquadService._validate_emblem(emblem)
        if err:
            return None, err

        try:
            tx_result, err = S1SquadService._create_squad_transaction(
                user_id=user_id,
                adventurer_id=adventurer_id,
                normalized_name=normalized_name,
                name_key=name_key,
                normalized_emblem=normalized_emblem,
            )
            if err:
                db_manager.rollback()
                return None, err
            squad_id = tx_result['squad_id']
            adv_id = tx_result['adventurer_id']
            S1SquadService._notify_changed(
                [SimpleNamespace(id=adv_id, owner_id=user_id)],
                'squad_created',
            )
            try:
                payload = S1SquadService._squad_detail(
                    squad_id,
                    current_adventurer_id=adv_id,
                )
            except Exception:
                logger.exception(
                    'S1 纹章小队创建已提交，但详情回读失败 squad_id=%s',
                    squad_id,
                )
                payload = None
            if payload is None:
                total_capacity = S1SquadService._total_capacity()
                payload = {
                    'id': squad_id,
                    'name': normalized_name,
                    'emblem': normalized_emblem,
                    'leader_adventurer_id': adv_id,
                    'leader_name': tx_result['leader_name'],
                    'member_count': 1,
                    'member_slots': S1SquadService._member_slots(),
                    'total_capacity': total_capacity,
                    'is_full': total_capacity <= 1,
                    'is_leader': True,
                    'members': [{
                        'adventurer_id': adv_id,
                        'name': tx_result['leader_name'],
                        'level': tx_result['leader_level'],
                        'character_class': tx_result['leader_class'],
                        'is_leader': True,
                        'role': 'leader',
                    }],
                }
            payload['gold_balance'] = tx_result['gold_balance']
            return payload, None
        except IntegrityError:
            db_manager.rollback()
            logger.info(
                '创建 S1 纹章小队发生唯一约束冲突 user_id=%s adventurer_id=%s',
                user_id,
                adventurer_id,
                exc_info=True,
            )
            return None, '小队名称已被使用，或创建状态发生冲突，请稍后重试'
        except ConcurrencyError:
            db_manager.rollback()
            logger.warning(
                '创建 S1 纹章小队遇到并发冲突 user_id=%s adventurer_id=%s',
                user_id,
                adventurer_id,
                exc_info=True,
            )
            return None, '创建纹章小队时系统繁忙，请稍后重试'
        except Exception:
            db_manager.rollback()
            logger.exception('创建 S1 纹章小队失败')
            return None, '创建纹章小队失败，请稍后重试'

    @staticmethod
    @db_retry(max_retries=3)
    def _create_squad_transaction(
        *,
        user_id: int,
        adventurer_id,
        normalized_name: str,
        name_key: str,
        normalized_emblem: dict,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """完整重试建队事务；禁止只重试扣款子步骤。"""
        prechecked_adv, err = S1SquadService._precheck_owned_adventurer(
            adventurer_id,
            user_id,
        )
        if err:
            return None, err
        parsed_adv_id = int(prechecked_adv.id)

        # 这些读取只是减少无意义的货币锁等待；唯一约束与锁后复核才是最终裁决。
        if prechecked_adv.squad_id is not None:
            return None, '该冒险者已经加入纹章小队'
        if AdventurerS1Squad.query.filter_by(
            leader_adventurer_id=parsed_adv_id,
        ).first():
            return None, '该冒险者已经创建过纹章小队'
        if AdventurerS1Squad.query.filter_by(name_key=name_key).first():
            return None, '这个小队名称已经被使用'

        from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
            S1CurrencyService,
        )

        gold_balance, deduct_err, currency_log = (
            S1CurrencyService.deduct_in_transaction(
                parsed_adv_id,
                'gold',
                S1SquadService._create_gold_cost(),
                source_type='squad_creation',
                related_type='squad',
                description=f'创建纹章小队「{normalized_name}」',
            )
        )
        if deduct_err:
            if '余额不足' in deduct_err:
                return (
                    None,
                    f'金币不足，创建纹章小队需要'
                    f'{S1SquadService._create_gold_cost():g}金币',
                )
            logger.error(
                'S1 纹章小队事务扣款失败 adventurer_id=%s error=%s',
                parsed_adv_id,
                deduct_err,
            )
            return None, '创建纹章小队失败，请稍后重试'

        # currency -> adventurer 的锁序后再次做全部权限与状态校验。若任一步失败，
        # 外层会回滚金币副本、账本、流水与后续小队写入。
        adv, err = S1SquadService._lock_owned_adventurer(
            parsed_adv_id,
            user_id,
        )
        if err:
            return None, err
        if adv.squad_id is not None:
            return None, '该冒险者已经加入纹章小队'
        if AdventurerS1Squad.query.filter_by(
            leader_adventurer_id=adv.id,
        ).first():
            return None, '该冒险者已经创建过纹章小队'
        if AdventurerS1Squad.query.filter_by(name_key=name_key).first():
            return None, '这个小队名称已经被使用'

        squad = AdventurerS1Squad(
            name=normalized_name,
            name_key=name_key,
            leader_adventurer_id=adv.id,
        )
        S1SquadService._apply_emblem(squad, normalized_emblem)
        db.session.add(squad)
        db.session.flush()

        adv.squad_id = squad.id
        if currency_log is not None:
            currency_log.related_id = str(squad.id)
        db.session.flush()

        result = {
            'squad_id': int(squad.id),
            'adventurer_id': int(adv.id),
            'leader_name': adv.name,
            'leader_level': int(adv.level or 1),
            'leader_class': adv.character_class,
            'gold_balance': gold_balance,
        }
        db.session.commit()
        return result, None

    @staticmethod
    def join_squad(
        user_id: int,
        adventurer_id,
        squad_id,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err
        try:
            adv, err = S1SquadService._lock_owned_adventurer(adventurer_id, user_id)
            if err:
                db_manager.rollback()
                return None, err
            if adv.squad_id is not None:
                db_manager.rollback()
                return None, '该冒险者已经加入纹章小队'

            squad, err = S1SquadService._lock_squad(squad_id)
            if err:
                db_manager.rollback()
                return None, err
            member_count = (
                db_manager.query(func.count(AdventurerS1.id))
                .filter(AdventurerS1.squad_id == squad.id)
                .scalar()
                or 0
            )
            if int(member_count) >= S1SquadService._total_capacity():
                db_manager.rollback()
                return (
                    None,
                    f'该小队已满（1名队长 + 最多'
                    f'{S1SquadService._member_slots()}名成员）',
                )

            adv.squad_id = squad.id
            payload = S1SquadService._squad_detail(
                squad.id,
                current_adventurer_id=adv.id,
            )
            changed_refs = S1SquadService._changed_refs([adv])
            ok, commit_err = db_manager.commit()
            if not ok:
                logger.error(
                    '加入 S1 纹章小队提交失败 adventurer_id=%s squad_id=%s error=%s',
                    adv.id,
                    squad.id,
                    commit_err,
                )
                return None, '加入纹章小队失败，请稍后重试'
            S1SquadService._notify_changed(changed_refs, 'squad_joined')
            return payload, None
        except Exception:
            db_manager.rollback()
            logger.exception('加入 S1 纹章小队失败')
            return None, '加入纹章小队失败，请稍后重试'

    @staticmethod
    def leave_squad(
        user_id: int,
        adventurer_id,
        squad_id,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err
        try:
            adv, err = S1SquadService._lock_owned_adventurer(
                adventurer_id,
                user_id,
                require_playable=False,
            )
            if err:
                db_manager.rollback()
                return None, err
            parsed_squad_id, err = S1SquadService._parse_positive_id(squad_id, '小队')
            if err:
                db_manager.rollback()
                return None, err
            if adv.squad_id != parsed_squad_id:
                db_manager.rollback()
                return None, '该冒险者不在这个纹章小队中'

            squad, err = S1SquadService._lock_squad(parsed_squad_id)
            if err:
                db_manager.rollback()
                return None, err
            if squad.leader_adventurer_id == adv.id:
                db_manager.rollback()
                return None, '队长不能直接离队，请先解散小队'

            adv.squad_id = None
            changed_refs = S1SquadService._changed_refs([adv])
            changed_squad_id = int(squad.id)
            ok, commit_err = db_manager.commit()
            if not ok:
                logger.error(
                    '退出 S1 纹章小队提交失败 adventurer_id=%s squad_id=%s error=%s',
                    adv.id,
                    squad.id,
                    commit_err,
                )
                return None, '退出纹章小队失败，请稍后重试'
            S1SquadService._notify_changed(changed_refs, 'squad_left')
            return {'squad_id': changed_squad_id, 'left': True}, None
        except Exception:
            db_manager.rollback()
            logger.exception('退出 S1 纹章小队失败')
            return None, '退出纹章小队失败，请稍后重试'

    @staticmethod
    def update_emblem(
        user_id: int,
        adventurer_id,
        squad_id,
        emblem,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err
        try:
            adv, err = S1SquadService._lock_owned_adventurer(
                adventurer_id,
                user_id,
                require_playable=False,
            )
            if err:
                db_manager.rollback()
                return None, err
            squad, err = S1SquadService._lock_squad(squad_id)
            if err:
                db_manager.rollback()
                return None, err
            if adv.squad_id != squad.id or squad.leader_adventurer_id != adv.id:
                db_manager.rollback()
                return None, '只有纹章小队队长可以修改纹章'

            normalized_emblem, err = S1SquadService._validate_emblem(
                emblem,
                base=squad.emblem_dict(),
                require_change=True,
            )
            if err:
                db_manager.rollback()
                return None, err
            S1SquadService._apply_emblem(squad, normalized_emblem)
            affected = S1SquadService._member_rows(squad.id)
            payload = S1SquadService._squad_detail(
                squad.id,
                current_adventurer_id=adv.id,
            )
            changed_refs = S1SquadService._changed_refs(affected)
            ok, commit_err = db_manager.commit()
            if not ok:
                logger.error(
                    '保存 S1 纹章小队纹章提交失败 squad_id=%s error=%s',
                    squad.id,
                    commit_err,
                )
                return None, '保存纹章失败，请稍后重试'
            S1SquadService._notify_changed(changed_refs, 'squad_emblem_updated')
            return payload, None
        except Exception:
            db_manager.rollback()
            logger.exception('修改 S1 纹章小队纹章失败')
            return None, '保存纹章失败，请稍后重试'

    @staticmethod
    def disband_squad(
        user_id: int,
        adventurer_id,
        squad_id,
    ) -> Tuple[Optional[dict], Optional[str]]:
        enabled_err = S1SquadService._require_enabled()
        if enabled_err:
            return None, enabled_err
        try:
            parsed_squad_id, err = S1SquadService._parse_positive_id(squad_id, '小队')
            if err:
                return None, err

            # 若恰有成员在“成员行 -> 小队行”的锁序中完成加入，第一次快照可能漏掉它。
            # 锁住小队后复核成员集合；发现新增成员就整体回滚并按同一锁序重试。
            for _attempt in range(3):
                leader, err = S1SquadService._lock_owned_adventurer(
                    adventurer_id,
                    user_id,
                    require_playable=False,
                )
                if err:
                    db_manager.rollback()
                    return None, err
                if leader.squad_id != parsed_squad_id:
                    db_manager.rollback()
                    return None, '该冒险者不在这个纹章小队中'

                affected = (
                    AdventurerS1.query
                    .populate_existing()
                    .options(
                        load_only(
                            AdventurerS1.id,
                            AdventurerS1.owner_id,
                            AdventurerS1.squad_id,
                        ),
                        noload('*'),
                    )
                    .filter(AdventurerS1.squad_id == parsed_squad_id)
                    .order_by(AdventurerS1.id.asc())
                    .with_for_update()
                    .all()
                )
                locked_ids = {member.id for member in affected}
                squad, err = S1SquadService._lock_squad(parsed_squad_id)
                if err:
                    db_manager.rollback()
                    return None, err
                if squad.leader_adventurer_id != leader.id:
                    db_manager.rollback()
                    return None, '只有纹章小队队长可以解散小队'

                current_ids = {
                    row[0]
                    for row in (
                        db_manager.query(AdventurerS1.id)
                        .filter(AdventurerS1.squad_id == parsed_squad_id)
                        .all()
                    )
                }
                if not current_ids.issubset(locked_ids):
                    db_manager.rollback()
                    continue

                for member in affected:
                    member.squad_id = None
                ok, flush_err = db_manager.flush()
                if not ok:
                    db_manager.rollback()
                    logger.error(
                        '解散 S1 纹章小队清成员失败 squad_id=%s error=%s',
                        parsed_squad_id,
                        flush_err,
                    )
                    return None, '解散纹章小队失败，请稍后重试'
                ok, delete_err = db_manager.delete_instance(squad, flush=False)
                if not ok:
                    db_manager.rollback()
                    logger.error(
                        '解散 S1 纹章小队删除失败 squad_id=%s error=%s',
                        parsed_squad_id,
                        delete_err,
                    )
                    return None, '解散纹章小队失败，请稍后重试'
                changed_refs = S1SquadService._changed_refs(affected)
                ok, commit_err = db_manager.commit()
                if not ok:
                    logger.error(
                        '解散 S1 纹章小队提交失败 squad_id=%s error=%s',
                        parsed_squad_id,
                        commit_err,
                    )
                    return None, '解散纹章小队失败，请稍后重试'
                S1SquadService._notify_changed(changed_refs, 'squad_disbanded')
                return {'squad_id': parsed_squad_id, 'disbanded': True}, None

            return None, '小队成员正在变动，请稍后重试解散'
        except Exception:
            db_manager.rollback()
            logger.exception('解散 S1 纹章小队失败')
            return None, '解散纹章小队失败，请稍后重试'
