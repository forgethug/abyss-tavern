"""S1 静态环境 NPC：无写入偶遇、隐藏关系、幂等互动与物品投喂。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, or_
from sqlalchemy.orm import load_only, noload

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1AmbientNpcGiftAiCache,
    AdventurerS1AmbientNpcInteraction,
    AdventurerS1AmbientNpcState,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.s1_item_types import normalize_item_type
from plugins.adventurer_s1.services.s1_ambient_npc_catalog import (
    AMBIENT_NPC_KEYS,
    AmbientNpcActionTemplate,
    AmbientNpcTemplate,
    AmbientNpcTextLine,
    get_ambient_npc_template,
)
from plugins.adventurer_s1.services.s1_item_instance_service import (
    ITEM_OPERATION_PLAYER_DESTRUCTIVE,
    consume_item,
    lock_warehouse_aggregate,
    validate_item_operation,
)
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    build_clue_attach_meta,
    is_catalog_immutable_item,
    sanitize_player_catalog_item_dict,
)
from utils.time_utils import BEIJING_TZ


AMBIENT_NPC_SURFACES = (
    'tavern',
    'activity',
    'building',
    'my-adventurers',
    'adventurers',
    'party',
    'logs',
)
SUPPORTED_SURFACES = frozenset(AMBIENT_NPC_SURFACES)
_ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
_REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


_GIFT_CACHE_PENDING_EFFECT = '__pending__'

AMBIENT_NPC_RETRY_SAME_REQUEST = 'retry_same_request'
AMBIENT_NPC_RESOLVED_BY_OTHER_REQUEST = 'resolved_by_other_request'
AMBIENT_NPC_TERMINAL = 'terminal'
_AMBIENT_NPC_RETRY_DISPOSITIONS = frozenset({
    AMBIENT_NPC_RETRY_SAME_REQUEST,
    AMBIENT_NPC_RESOLVED_BY_OTHER_REQUEST,
    AMBIENT_NPC_TERMINAL,
})


@dataclass(frozen=True)
class AmbientNpcInteractionError:
    """环境 NPC 动作的可公开结构化失败合同。"""

    message: str
    error_code: str
    retry_disposition: str
    request_id: str
    encounter_id: str
    action_key: str
    surface: str
    http_status: int = 409
    canonical_payload: dict | None = None

    def __post_init__(self) -> None:
        if self.retry_disposition not in _AMBIENT_NPC_RETRY_DISPOSITIONS:
            raise ValueError('未知的环境 NPC 重试处置')

    def to_public_data(self) -> dict:
        data = {
            'error_code': self.error_code,
            'retry_disposition': self.retry_disposition,
            'request_id': self.request_id,
            'encounter_id': self.encounter_id,
            'action_key': self.action_key,
            'surface': self.surface,
        }
        if self.canonical_payload is not None:
            data['canonical_payload'] = self.canonical_payload
        return data


class S1AmbientNpcService:
    """模板驱动的环境 NPC 服务；公共输出永不包含好感分数或内部档位。"""

    _HANDLERS = {
        'static_text': '_handle_static_text',
        'consume_gift_ai': '_handle_consume_gift_ai',
    }

    @staticmethod
    def _config_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(current_app.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _window_seconds() -> int:
        return S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_WINDOW_SECONDS', 3600, 60, 86400,
        )

    @staticmethod
    def _normalize_surface(surface_key: str) -> str | None:
        value = str(surface_key or '').strip().lower()
        return value if value in SUPPORTED_SURFACES else None

    @staticmethod
    def _secret() -> bytes | None:
        value = current_app.config.get('SECRET_KEY')
        if not value:
            return None
        return str(value).encode('utf-8')

    @staticmethod
    def _active_owned_adventurer_id(user_id: int) -> int | None:
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
        )

        adventurer_id, _ = AdventurerS1Service.resolve_active_adventurer_id(user_id)
        return adventurer_id

    @staticmethod
    def _lock_active_owned_adventurer(user_id: int):
        adventurer_id = S1AmbientNpcService._active_owned_adventurer_id(user_id)
        if not adventurer_id:
            return None
        return (
            AdventurerS1.query
            .populate_existing()
            .options(
                load_only(
                    AdventurerS1.id,
                    AdventurerS1.owner_id,
                    AdventurerS1.status,
                    AdventurerS1.is_player_created,
                ),
                noload('*'),
            )
            .filter(
                AdventurerS1.id == adventurer_id,
                AdventurerS1.owner_id == user_id,
                AdventurerS1.is_player_created == True,
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .with_for_update()
            .first()
        )

    @staticmethod
    def _affinity_tier(score: int) -> str:
        familiar = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_FAMILIAR_SCORE', 10, -99, 100,
        )
        close = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_CLOSE_SCORE', 30, familiar, 100,
        )
        trusted = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_TRUSTED_SCORE', 60, close, 100,
        )
        if score >= trusted:
            return 'trusted'
        if score >= close:
            return 'close'
        if score >= familiar:
            return 'familiar'
        return 'wary'

    @staticmethod
    def _window_bucket(now_ts: int | float | None = None) -> int:
        timestamp = int(time.time() if now_ts is None else now_ts)
        return timestamp // S1AmbientNpcService._window_seconds()

    @staticmethod
    def _encounter_assignments(bucket: int, secret: bytes) -> dict[str, str]:
        """计算全服共享的 surface -> NPC 映射；玩家身份不参与出现与地点。"""
        appearance_percent = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_APPEARANCE_PERCENT', 15, 0, 100,
        )
        appearing = []
        for npc_key in AMBIENT_NPC_KEYS:
            seed = f'ambient-npc:appearance:v3:{bucket}:{npc_key}'.encode('utf-8')
            appearance_digest = hmac.new(secret, seed, hashlib.sha256).digest()
            appearance_score = int.from_bytes(appearance_digest[:2], 'big') % 100
            if appearance_score >= appearance_percent:
                continue
            priority_digest = hmac.new(
                secret,
                f'ambient-npc:priority:v3:{bucket}:{npc_key}'.encode('utf-8'),
                hashlib.sha256,
            ).digest()
            appearing.append((priority_digest, npc_key))

        assignments = {}
        available_surfaces = set(AMBIENT_NPC_SURFACES)
        for _priority, npc_key in sorted(appearing, reverse=True):
            if not available_surfaces:
                break
            surface = max(
                available_surfaces,
                key=lambda candidate: (
                    hmac.new(
                        secret,
                        (
                            f'ambient-npc:surface:v3:{bucket}:{npc_key}:'
                            f'{candidate}'
                        ).encode('utf-8'),
                        hashlib.sha256,
                    ).digest(),
                    candidate,
                ),
            )
            assignments[surface] = npc_key
            available_surfaces.remove(surface)
        return assignments

    @staticmethod
    def _encounter_for_bucket(
        adventurer_id: int,
        surface_key: str,
        bucket: int,
    ) -> dict | None:
        secret = S1AmbientNpcService._secret()
        if not secret:
            return None
        npc_key = S1AmbientNpcService._encounter_assignments(
            bucket,
            secret,
        ).get(surface_key)
        if not npc_key:
            return None
        template = get_ambient_npc_template(npc_key)
        if not template:
            return None
        # 玩家身份只绑定互动凭据与出现文本，不参与 NPC 是否出现及出现地点。
        seed = (
            f'ambient-npc:encounter:v4:{adventurer_id}:{bucket}:'
            f'{surface_key}:{npc_key}'
        ).encode('utf-8')
        digest = hmac.new(secret, seed, hashlib.sha256).digest()
        encounter_id = hmac.new(
            secret,
            (
                b'encounter:v4:'
                + seed
                + b':'
                + str(template.revision).encode('ascii')
            ),
            hashlib.sha256,
        ).hexdigest()[:32]
        return {
            'id': encounter_id,
            'npc_key': npc_key,
            'surface_key': surface_key,
            'bucket': bucket,
            'digest': digest,
        }

    @staticmethod
    def _resolve_encounter(
        adventurer_id: int,
        surface_key: str,
        encounter_id: str,
        *,
        now_ts: int | float | None = None,
    ) -> dict | None:
        current_bucket = S1AmbientNpcService._window_bucket(now_ts)
        for bucket in (current_bucket, current_bucket - 1):
            encounter = S1AmbientNpcService._encounter_for_bucket(
                adventurer_id, surface_key, bucket,
            )
            if encounter and hmac.compare_digest(encounter['id'], encounter_id):
                return encounter
        return None

    @staticmethod
    def _deterministic_presence_line(
        encounter: dict,
        template: AmbientNpcTemplate,
        tier: str,
    ) -> AmbientNpcTextLine:
        secret = S1AmbientNpcService._secret() or b''
        digest = hmac.new(
            secret,
            encounter['digest'] + f':presence:{template.revision}:{tier}'.encode('utf-8'),
            hashlib.sha256,
        ).digest()
        pool = template.presence[tier]
        return pool[int.from_bytes(digest[:4], 'big') % len(pool)]

    @staticmethod
    def _receipt_projection(receipt: AdventurerS1AmbientNpcInteraction) -> dict:
        consumed_item = None
        response_text = receipt.response_text
        if receipt.item_name:
            consumed_item = {
                'source': receipt.item_source,
                'id': receipt.item_id,
                'name': receipt.item_name,
                'quantity': 1,
                'remaining_quantity': receipt.item_remaining_quantity,
                'removed': receipt.item_removed,
            }
            catalog_item = S1AmbientNpcService._catalog_items_by_name(
                (receipt.item_name,)
            ).get(receipt.item_name)
            clue_fields = build_clue_attach_meta(catalog_item)
            if clue_fields:
                sanitize_player_catalog_item_dict(
                    consumed_item,
                    clue_fields,
                    name_key='name',
                )
                response_text = str(response_text or '').replace(
                    receipt.item_name,
                    consumed_item['name'],
                )
        return {
            'encounter_id': receipt.encounter_id,
            'action_key': receipt.action_key,
            'text': response_text,
            'consumed_item': consumed_item,
            'completed': True,
        }

    @staticmethod
    def _interaction_response(
        user_id: int,
        surface_key: str,
        interaction: dict,
        *,
        now_ts: int | float | None = None,
    ) -> dict:
        current, _ = S1AmbientNpcService.get_current(
            user_id, surface_key, now_ts=now_ts,
        )
        encounter = (current or {}).get('encounter')
        refresh_required = (
            not encounter
            or encounter.get('id') != interaction.get('encounter_id')
        )
        if refresh_required:
            encounter = None
        return {
            'interaction': interaction,
            'encounter': encounter,
            'next_refresh_at': (current or {}).get('next_refresh_at'),
            'refresh_required': refresh_required,
        }

    @staticmethod
    def _interaction_error(
        *,
        message: str,
        error_code: str,
        retry_disposition: str,
        request_id: str,
        encounter_id: str,
        action_key: str,
        surface: str,
        canonical_payload: dict | None = None,
        http_status: int = 409,
    ) -> AmbientNpcInteractionError:
        return AmbientNpcInteractionError(
            message=message,
            error_code=error_code,
            retry_disposition=retry_disposition,
            request_id=request_id,
            encounter_id=encounter_id,
            action_key=action_key,
            surface=surface,
            canonical_payload=canonical_payload,
            http_status=http_status,
        )

    @staticmethod
    def _other_request_completed_error(
        user_id: int,
        surface: str,
        encounter_id: str,
        action_key: str,
        request_id: str,
        receipt: AdventurerS1AmbientNpcInteraction,
        *,
        now_ts: int | float | None = None,
    ) -> AmbientNpcInteractionError:
        result = S1AmbientNpcService._receipt_projection(receipt)
        canonical_payload = S1AmbientNpcService._interaction_response(
            user_id,
            surface,
            result,
            now_ts=now_ts,
        )
        return S1AmbientNpcService._interaction_error(
            message='这次互动已由另一处请求完成，已同步实际结果',
            error_code='ambient_npc_action_completed_by_other_request',
            retry_disposition=AMBIENT_NPC_RESOLVED_BY_OTHER_REQUEST,
            request_id=request_id,
            encounter_id=encounter_id,
            action_key=action_key,
            surface=surface,
            canonical_payload=canonical_payload,
        )

    @staticmethod
    def _next_refresh_at(bucket: int) -> str:
        timestamp = (bucket + 1) * S1AmbientNpcService._window_seconds()
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')

    @staticmethod
    def _now_utc(now_ts: int | float | None = None) -> datetime:
        if now_ts is None:
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(float(now_ts), timezone.utc)

    @staticmethod
    def _interaction_day(now_ts: int | float | None = None):
        return S1AmbientNpcService._now_utc(now_ts).astimezone(BEIJING_TZ).date()

    @staticmethod
    def _next_daily_reset_at(now_ts: int | float | None = None) -> str:
        next_day = S1AmbientNpcService._interaction_day(now_ts) + timedelta(days=1)
        value = datetime.combine(next_day, datetime_time.min, tzinfo=BEIJING_TZ)
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    @staticmethod
    def get_current(
        user_id: int,
        surface_key: str,
        *,
        now_ts: int | float | None = None,
    ) -> tuple[dict | None, str | None]:
        surface = S1AmbientNpcService._normalize_surface(surface_key)
        if not surface:
            return None, '无效的 NPC 偶遇界面'
        bucket = S1AmbientNpcService._window_bucket(now_ts)
        empty = {
            'encounter': None,
            'next_refresh_at': S1AmbientNpcService._next_refresh_at(bucket),
        }
        if not current_app.config.get('ADVENTURER_S1_AMBIENT_NPC_ENABLED', True):
            return empty, None
        adventurer_id = S1AmbientNpcService._active_owned_adventurer_id(user_id)
        if not adventurer_id:
            return empty, None
        encounter = S1AmbientNpcService._encounter_for_bucket(
            adventurer_id, surface, bucket,
        )
        if not encounter:
            return empty, None
        template = get_ambient_npc_template(encounter['npc_key'])
        if not template:
            return empty, None
        state = (
            AdventurerS1AmbientNpcState.query
            .options(
                load_only(AdventurerS1AmbientNpcState.affinity_score),
                noload('*'),
            )
            .filter_by(adventurer_id=adventurer_id, npc_key=template.key)
            .first()
        )
        tier = S1AmbientNpcService._affinity_tier(state.affinity_score if state else 0)
        presence = S1AmbientNpcService._deterministic_presence_line(
            encounter, template, tier,
        )
        daily_keys = tuple(sorted({
            action.daily_limit_key
            for action in template.actions.values()
            if action.daily_limit_key
        }))
        receipt_filter = (
            AdventurerS1AmbientNpcInteraction.encounter_id == encounter['id']
        )
        if daily_keys:
            receipt_filter = or_(
                receipt_filter,
                and_(
                    AdventurerS1AmbientNpcInteraction.npc_key == template.key,
                    AdventurerS1AmbientNpcInteraction.interaction_day
                    == S1AmbientNpcService._interaction_day(now_ts),
                    AdventurerS1AmbientNpcInteraction.daily_limit_key.in_(daily_keys),
                ),
            )
        receipts = (
            AdventurerS1AmbientNpcInteraction.query
            .options(
                load_only(
                    AdventurerS1AmbientNpcInteraction.encounter_id,
                    AdventurerS1AmbientNpcInteraction.action_key,
                    AdventurerS1AmbientNpcInteraction.daily_limit_key,
                    AdventurerS1AmbientNpcInteraction.response_text,
                    AdventurerS1AmbientNpcInteraction.item_source,
                    AdventurerS1AmbientNpcInteraction.item_id,
                    AdventurerS1AmbientNpcInteraction.item_name,
                ),
                noload('*'),
            )
            .filter(
                AdventurerS1AmbientNpcInteraction.adventurer_id == adventurer_id,
                receipt_filter,
            )
            .all()
        )
        encounter_completed = {
            receipt.action_key: receipt
            for receipt in receipts
            if receipt.encounter_id == encounter['id']
        }
        daily_completed = {
            receipt.daily_limit_key: receipt
            for receipt in receipts
            if receipt.daily_limit_key
        }
        actions = []
        for action in template.actions.values():
            action_key = action.key
            receipt = encounter_completed.get(action_key)
            if not receipt and action.daily_limit_key:
                receipt = daily_completed.get(action.daily_limit_key)
            is_completed = receipt is not None
            daily_blocked = bool(is_completed and action.daily_limit_key)
            actions.append({
                'key': action.key,
                'label': action.labels[tier],
                'requires_item': action.requires_item,
                'completed': is_completed,
                'available': not is_completed,
                'unavailable_reason': (
                    '今天已经赠过礼物，明天再来看看吧'
                    if daily_blocked
                    else ('这次已经回应过了' if is_completed else None)
                ),
                'next_available_at': (
                    S1AmbientNpcService._next_daily_reset_at(now_ts)
                    if daily_blocked
                    else None
                ),
                'last_text': receipt.response_text if receipt else None,
            })
        return {
            'encounter': {
                'id': encounter['id'],
                'surface_key': surface,
                'npc': {
                    'key': template.key,
                    'name': template.name,
                    'breed': template.breed,
                    'icon_key': template.icon_key,
                    'icon': template.icon,
                    'short_intro': template.short_intro,
                    'public_background': template.public_background,
                    'presence_text': presence.text,
                },
                'actions': actions,
            },
            'next_refresh_at': S1AmbientNpcService._next_refresh_at(bucket),
        }, None

    @staticmethod
    def _get_action_context(
        user_id: int,
        surface_key: str,
        encounter_id: str,
        action_key: str,
        *,
        for_update: bool,
        now_ts: int | float | None = None,
    ):
        surface = S1AmbientNpcService._normalize_surface(surface_key)
        if not surface:
            return None, None, None, None, '无效的 NPC 偶遇界面'
        if not _ID_PATTERN.fullmatch(str(encounter_id or '')):
            return None, None, None, None, '无效的 NPC 偶遇标识'
        if for_update:
            adventurer = S1AmbientNpcService._lock_active_owned_adventurer(user_id)
            adventurer_id = adventurer.id if adventurer else None
        else:
            adventurer = None
            adventurer_id = S1AmbientNpcService._active_owned_adventurer_id(user_id)
        if not adventurer_id:
            return None, None, None, None, '需要拥有存活的自建冒险者'
        encounter = S1AmbientNpcService._resolve_encounter(
            adventurer_id, surface, encounter_id, now_ts=now_ts,
        )
        if not encounter:
            return adventurer_id, None, None, None, '这次偶遇已经结束，猫猫去了别处'
        template = get_ambient_npc_template(encounter['npc_key'])
        action = template.actions.get(str(action_key or '').strip()) if template else None
        if not template or not action:
            return adventurer_id, encounter, template, None, '这个互动选项不存在'
        return adventurer_id, encounter, template, action, None

    @staticmethod
    def list_action_items(
        user_id: int,
        surface_key: str,
        encounter_id: str,
        action_key: str,
        *,
        now_ts: int | float | None = None,
    ) -> tuple[dict | None, str | None]:
        if not current_app.config.get('ADVENTURER_S1_AMBIENT_NPC_ENABLED', True):
            return None, '环境伙伴功能暂未开放'
        adventurer_id, _, template, action, err = S1AmbientNpcService._get_action_context(
            user_id,
            surface_key,
            encounter_id,
            action_key,
            for_update=False,
            now_ts=now_ts,
        )
        if err:
            return None, err
        if not action.requires_item:
            return None, '这个互动不需要选择物品'
        if action.daily_limit_key:
            daily_receipt = (
                AdventurerS1AmbientNpcInteraction.query
                .filter_by(
                    adventurer_id=adventurer_id,
                    npc_key=template.key,
                    daily_limit_key=action.daily_limit_key,
                    interaction_day=S1AmbientNpcService._interaction_day(now_ts),
                )
                .first()
            )
            if daily_receipt:
                return None, '今天已经赠过礼物，明天再来看看吧'
        carry = (
            AdventurerS1Equipment.query
            .options(
                load_only(
                    AdventurerS1Equipment.id,
                    AdventurerS1Equipment.name,
                    AdventurerS1Equipment.type,
                    AdventurerS1Equipment.rarity,
                    AdventurerS1Equipment.custom_desc,
                    AdventurerS1Equipment.uses_left,
                    AdventurerS1Equipment.damage_stage,
                    AdventurerS1Equipment.damage_note,
                    AdventurerS1Equipment.evolution_stage,
                    AdventurerS1Equipment.quantity,
                    AdventurerS1Equipment.created_at,
                    AdventurerS1Equipment.is_player_locked,
                ),
                noload('*'),
            )
            .filter(
                AdventurerS1Equipment.adventurer_id == adventurer_id,
                AdventurerS1Equipment.quantity > 0,
            )
            .order_by(AdventurerS1Equipment.id)
            .all()
        )
        warehouse = (
            AdventurerS1WarehouseItem.query
            .options(
                load_only(
                    AdventurerS1WarehouseItem.id,
                    AdventurerS1WarehouseItem.name,
                    AdventurerS1WarehouseItem.type,
                    AdventurerS1WarehouseItem.rarity,
                    AdventurerS1WarehouseItem.custom_desc,
                    AdventurerS1WarehouseItem.uses_left,
                    AdventurerS1WarehouseItem.evolution_stage,
                    AdventurerS1WarehouseItem.quantity,
                    AdventurerS1WarehouseItem.created_at,
                    AdventurerS1WarehouseItem.is_player_locked,
                ),
                noload('*'),
            )
            .filter(
                AdventurerS1WarehouseItem.adventurer_id == adventurer_id,
                AdventurerS1WarehouseItem.quantity > 0,
            )
            .order_by(AdventurerS1WarehouseItem.id)
            .all()
        )
        names = {item.name for item in carry}
        names.update(item.name for item in warehouse)
        catalog_items = S1AmbientNpcService._catalog_items_by_name(names)
        items = []
        for source, rows in (('carry', carry), ('warehouse', warehouse)):
            for item in rows:
                catalog_item = catalog_items.get(item.name)
                snapshot, semantic = S1AmbientNpcService._build_item_snapshot(
                    source,
                    item,
                    catalog_item,
                )
                level, warning = S1AmbientNpcService._confirmation_for_item(
                    item,
                    catalog_item,
                )
                item_payload = {
                    'source': source,
                    'id': item.id,
                    'name': item.name,
                    'type': semantic['type'],
                    'rarity': item.rarity or 'common',
                    'quantity': int(item.quantity or 0),
                    'created_at': item.created_at.isoformat() if item.created_at else None,
                    # 列表只做展示；最终 AI 输入会从锁定行重新生成完整未截断快照。
                    'description': semantic['effective_description'],
                    'uses_left': snapshot['uses_left'],
                    'evolution_stage': snapshot['evolution_stage'],
                    'confirmation_level': level,
                    'warning_text': warning,
                    'is_player_locked': bool(item.is_player_locked),
                }
                sanitize_player_catalog_item_dict(
                    item_payload,
                    build_clue_attach_meta(catalog_item),
                    name_key='name',
                    desc_key='description',
                    type_key='type',
                )
                items.append(item_payload)
        return {'items': items}, None

    @staticmethod
    def _catalog_items_by_name(names) -> dict[str, AdventurerS1Item]:
        clean_names = tuple(sorted({str(name) for name in names if str(name or '').strip()}))
        if not clean_names:
            return {}
        rows = (
            AdventurerS1Item.query
            .options(
                load_only(
                    AdventurerS1Item.id,
                    AdventurerS1Item.name,
                    AdventurerS1Item.type,
                    AdventurerS1Item.description,
                    AdventurerS1Item.rarity,
                    AdventurerS1Item.use_condition,
                    AdventurerS1Item.world_unique,
                ),
                noload('*'),
            )
            .filter(AdventurerS1Item.name.in_(clean_names))
            .all()
        )
        return {row.name: row for row in rows}

    @staticmethod
    def _build_item_snapshot(
        source: str,
        item,
        catalog_item: AdventurerS1Item | None,
    ) -> tuple[dict, dict]:
        custom_desc = getattr(item, 'custom_desc', None)
        effective_description = (
            str(custom_desc)
            if custom_desc is not None
            else str(getattr(catalog_item, 'description', '') or '')
        )
        effective_type = str(
            item.type
            or getattr(catalog_item, 'type', None)
            or 'unknown'
        )
        semantic = {
            'name': str(item.name or ''),
            'type': effective_type,
            'rarity': str(item.rarity or 'common'),
            'effective_description': effective_description,
            'uses_left': getattr(item, 'uses_left', None),
            'evolution_stage': int(getattr(item, 'evolution_stage', 0) or 0),
            'damage_stage': str(getattr(item, 'damage_stage', '') or ''),
            'damage_note': str(getattr(item, 'damage_note', '') or ''),
            'world_unique': bool(getattr(catalog_item, 'world_unique', False)),
            'immutable': bool(
                catalog_item
                and is_catalog_immutable_item(catalog_item.use_condition)
            ),
        }
        snapshot = {
            'source': source,
            'id': int(item.id),
            'quantity': int(item.quantity or 0),
            **semantic,
        }
        return snapshot, semantic

    @staticmethod
    def _semantic_snapshot_hash(semantic: dict) -> str:
        payload = json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _confirmation_for_item(item, catalog_item: AdventurerS1Item | None):
        normalized_type = normalize_item_type(
            item.type
            or getattr(catalog_item, 'type', None)
            or 'unknown'
        )
        rarity = str(item.rarity or 'common').strip().lower()
        irreversible = bool(
            normalized_type == 'companion'
            or getattr(catalog_item, 'world_unique', False)
            or (
                catalog_item
                and is_catalog_immutable_item(catalog_item.use_condition)
            )
        )
        if irreversible:
            return (
                'irreversible',
                '这是不可替代或受保护的特殊物品。无论对方接受还是拒绝，赠出 1 件后都会永久消耗且无法找回，请输入完整物品名确认。',
            )
        valuable = bool(
            rarity in {'rare', 'epic', 'legendary'}
            or getattr(item, 'custom_desc', None) is not None
            or int(getattr(item, 'evolution_stage', 0) or 0) > 0
        )
        if valuable:
            return (
                'valuable',
                '这件物品较为珍贵。无论对方接受还是拒绝，赠出 1 件后都会永久消耗且无法找回。',
            )
        return 'normal', '无论对方接受还是拒绝，赠出后都会永久消耗 1 件物品，且无法撤回。'

    @staticmethod
    def _normalize_item_ref(item_ref) -> tuple[dict | None, str | None]:
        if not isinstance(item_ref, dict):
            return None, '请选择要赠送的物品'
        source = str(item_ref.get('source') or '').strip()
        if source not in {'carry', 'warehouse'}:
            return None, '无效的物品来源'
        try:
            item_id = int(item_ref.get('id'))
        except (TypeError, ValueError):
            return None, '无效的物品 id'
        if item_id <= 0:
            return None, '无效的物品 id'
        if 'quantity' in item_ref:
            try:
                quantity = int(item_ref.get('quantity'))
            except (TypeError, ValueError):
                return None, '赠礼数量无效'
            if quantity != 1:
                return None, '每次只能赠送 1 件物品'
        return {'source': source, 'id': item_id}, None

    @staticmethod
    def _load_selected_item_snapshot(
        adventurer_id: int,
        item_ref,
        *,
        for_update: bool,
    ) -> tuple[object | None, dict | None, dict | None, str | None, str | None]:
        normalized_ref, ref_err = S1AmbientNpcService._normalize_item_ref(item_ref)
        if ref_err:
            return None, None, None, None, ref_err
        source = normalized_ref['source']
        model = AdventurerS1Equipment
        columns = [
            AdventurerS1Equipment.id,
            AdventurerS1Equipment.adventurer_id,
            AdventurerS1Equipment.name,
            AdventurerS1Equipment.type,
            AdventurerS1Equipment.rarity,
            AdventurerS1Equipment.custom_desc,
            AdventurerS1Equipment.uses_left,
            AdventurerS1Equipment.damage_stage,
            AdventurerS1Equipment.damage_note,
            AdventurerS1Equipment.evolution_stage,
            AdventurerS1Equipment.quantity,
            AdventurerS1Equipment.is_player_locked,
        ]
        if source == 'warehouse':
            if for_update:
                lock_warehouse_aggregate(adventurer_id)
            model = AdventurerS1WarehouseItem
            columns = [
                AdventurerS1WarehouseItem.id,
                AdventurerS1WarehouseItem.adventurer_id,
                AdventurerS1WarehouseItem.name,
                AdventurerS1WarehouseItem.type,
                AdventurerS1WarehouseItem.rarity,
                AdventurerS1WarehouseItem.custom_desc,
                AdventurerS1WarehouseItem.uses_left,
                AdventurerS1WarehouseItem.evolution_stage,
                AdventurerS1WarehouseItem.quantity,
                AdventurerS1WarehouseItem.is_player_locked,
            ]
        query = (
            model.query
            .options(load_only(*columns), noload('*'))
            .filter_by(adventurer_id=adventurer_id, id=normalized_ref['id'])
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        item = query.first()
        if not item or int(item.quantity or 0) <= 0:
            return None, None, None, None, '未找到指定物品'
        protection_err = validate_item_operation(
            item,
            ITEM_OPERATION_PLAYER_DESTRUCTIVE,
            action_label='向环境伙伴赠礼',
        )
        if protection_err:
            return None, None, None, None, protection_err
        catalog_item = S1AmbientNpcService._catalog_items_by_name((item.name,)).get(
            item.name
        )
        snapshot, semantic = S1AmbientNpcService._build_item_snapshot(
            source,
            item,
            catalog_item,
        )
        semantic_hash = S1AmbientNpcService._semantic_snapshot_hash(semantic)
        return item, snapshot, semantic, semantic_hash, None

    @staticmethod
    def _gift_affinity_delta(effect: str) -> int:
        configured = current_app.config.get(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_AFFINITY_EFFECTS',
            {},
        )
        if not isinstance(configured, dict):
            configured = {}
        defaults = {
            'strong_positive': 5,
            'positive': 3,
            'neutral': 0,
            'negative': -2,
        }
        try:
            value = int(configured.get(effect, defaults.get(effect, 0)))
        except (TypeError, ValueError):
            value = defaults.get(effect, 0)
        return max(-100, min(100, value))

    @staticmethod
    def _fallback_gift_result(
        template: AmbientNpcTemplate,
        tier: str,
        *,
        failure_stage: str | None,
        metadata: dict | None,
    ) -> dict:
        closeness = {
            'wary': '仍谨慎地与你保持着一小段距离',
            'familiar': '已经认出送礼的人，没有立刻退回阴影',
            'close': '自然地留在你身边，认真端详这份心意',
            'trusted': '毫无戒备地依偎过来，把礼物留在最安心的位置',
        }[tier]
        name = template.name
        texts = [
            f'{name}先绕着礼物看了一圈，{closeness}，最后用鼻尖轻轻碰了碰它。',
            f'{name}把礼物拨到爪边，安静研究了很久，随后抬眼朝你缓慢眨了一下。',
            f'{name}闻过礼物上的气息，又凑近你的手背确认片刻，尾尖终于柔和地落了下来。',
            f'{name}没有急着表态，只郑重地守在礼物旁；过了一会儿，它把前爪轻搭在你的近处。',
            f'{name}将礼物拖到自己惯常停留的位置，回头望了你一眼，像是已经把这份心意记住。',
            f'{name}围着礼物慢慢走了一圈，最终在它旁边伏下，喉间传来若有若无的呼噜声。',
            f'{name}先看礼物，又看向你，随后用脸颊轻轻擦过它，把熟悉的气味留在上面。',
            f'{name}把礼物收进两只前爪之间，安稳地停了下来，没有让其他脚步靠近这份心意。',
        ]
        configured = str(current_app.config.get(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_FALLBACK_EFFECT',
            'neutral',
        ) or 'neutral').strip()
        if configured not in {
            'strong_positive', 'positive', 'neutral', 'negative',
        }:
            configured = 'neutral'
        audit = dict(metadata or {})
        audit['fallback_used'] = True
        return {
            'affinity_effect': configured,
            'response_texts': texts,
            'source': 'fallback',
            'cache_key': None,
            'failure_stage': failure_stage,
            'metadata': audit,
        }

    @staticmethod
    def _normalized_gift_cache_row(
        row: AdventurerS1AmbientNpcGiftAiCache | None,
    ) -> dict | None:
        if not row or row.expires_at <= _portable_utc_now():
            return None
        effect = str(row.affinity_effect or '').strip()
        texts = row.response_texts if isinstance(row.response_texts, list) else []
        normalized_texts = [str(value).strip() for value in texts]
        if (
            effect not in {'strong_positive', 'positive', 'neutral', 'negative'}
            or not 6 <= len(normalized_texts) <= 8
            or any(not value for value in normalized_texts)
            or len(set(normalized_texts)) != len(normalized_texts)
        ):
            return None
        return {
            'affinity_effect': effect,
            'response_texts': normalized_texts,
            'source': 'cache',
            'cache_key': row.cache_key,
            'failure_stage': None,
            'metadata': {
                'model': row.model,
                'provider': row.provider,
                'profile_id': row.profile_id,
                'provider_request_id': row.provider_request_id,
                'prompt_hash': row.prompt_hash,
                'system_prompt_hash': row.system_prompt_hash,
                'contract_hash': row.contract_hash,
                'request_hash': row.request_hash,
                'origin_correlation_id': row.origin_correlation_id,
                'cache_hit': True,
            },
        }

    @staticmethod
    def _load_gift_cache(cache_key: str) -> dict | None:
        try:
            row = AdventurerS1AmbientNpcGiftAiCache.query.filter_by(
                cache_key=cache_key,
            ).first()
            return S1AmbientNpcService._normalized_gift_cache_row(row)
        finally:
            db_manager.remove()

    @staticmethod
    def _daily_gift_generation_cache_key(
        adventurer_id: int,
        npc_key: str,
        daily_limit_key: str,
        interaction_day: date,
    ) -> str:
        raw = (
            'adventurer_s1:ambient_npc:daily_gift_generation:v1|'
            f'{int(adventurer_id)}|{npc_key}|{daily_limit_key}|'
            f'{interaction_day.isoformat()}'
        )
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _lock_gift_generation_guard(
        cache_key: str,
        generation_token: str,
    ) -> bool:
        """最终短事务内确认每日 AI 调用租约仍属于当前 worker。"""
        row = (
            AdventurerS1AmbientNpcGiftAiCache.query
            .populate_existing()
            .filter_by(cache_key=cache_key)
            .with_for_update()
            .first()
        )
        return bool(
            row is not None
            and row.affinity_effect == _GIFT_CACHE_PENDING_EFFECT
            and row.provider_request_id == generation_token
            and row.expires_at > _portable_utc_now()
        )

    @staticmethod
    def _claim_gift_cache_generation(
        *,
        cache_key: str,
        template: AmbientNpcTemplate,
        prompt_revision: int,
        tier: str,
        semantic_hash: str,
        semantic: dict,
        request_id: str,
    ) -> tuple[dict | None, str | None]:
        """用缓存行短租约跨 worker 单飞；租约事务不包含 provider 调用。"""
        now = _portable_utc_now()
        configured_lease_seconds = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_AI_CACHE_LEASE_SECONDS',
            150,
            15,
            300,
        )
        ai_timeout_seconds = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_AI_TIMEOUT_SECONDS',
            45,
            10,
            120,
        )
        lease_seconds = max(configured_lease_seconds, ai_timeout_seconds + 30)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        generation_token = secrets.token_urlsafe(24)
        try:
            db_manager.remove()
            row = (
                AdventurerS1AmbientNpcGiftAiCache.query
                .filter_by(cache_key=cache_key)
                .with_for_update()
                .first()
            )
            cached = S1AmbientNpcService._normalized_gift_cache_row(row)
            if cached:
                db_manager.rollback()
                return cached, None
            if (
                row is not None
                and row.affinity_effect == _GIFT_CACHE_PENDING_EFFECT
                and row.expires_at > now
            ):
                db_manager.rollback()
                return None, None
            if row is None:
                row = AdventurerS1AmbientNpcGiftAiCache(
                    cache_key=cache_key,
                    npc_key=template.key,
                    template_revision=template.revision,
                    prompt_revision=int(prompt_revision),
                    affinity_tier=tier,
                    item_snapshot_hash=semantic_hash,
                    item_semantic_snapshot=dict(semantic),
                    affinity_effect=_GIFT_CACHE_PENDING_EFFECT,
                    response_texts=[],
                    provider_request_id=generation_token,
                    origin_correlation_id=request_id,
                    expires_at=lease_expires_at,
                )
                ok, add_err = db_manager.add(row)
                if not ok:
                    current_app.logger.info(
                        'S1 环境 NPC 赠礼缓存租约被并发占用 key=%s: %s',
                        cache_key,
                        add_err,
                    )
                    db_manager.rollback()
                    return S1AmbientNpcService._load_gift_cache(cache_key), None
            else:
                row.npc_key = template.key
                row.template_revision = template.revision
                row.prompt_revision = int(prompt_revision)
                row.affinity_tier = tier
                row.item_snapshot_hash = semantic_hash
                row.item_semantic_snapshot = dict(semantic)
                row.affinity_effect = _GIFT_CACHE_PENDING_EFFECT
                row.response_texts = []
                row.model = None
                row.provider = None
                row.profile_id = None
                row.provider_request_id = generation_token
                row.origin_correlation_id = request_id
                row.prompt_hash = None
                row.system_prompt_hash = None
                row.contract_hash = None
                row.request_hash = None
                row.usage_snapshot = None
                row.expires_at = lease_expires_at
            ok, commit_err = db_manager.commit()
            if not ok:
                current_app.logger.warning(
                    'S1 环境 NPC 赠礼缓存租约提交失败 key=%s: %s',
                    cache_key,
                    commit_err,
                )
                db_manager.rollback()
                return None, None
            return None, generation_token
        except Exception:
            current_app.logger.exception(
                'S1 环境 NPC 赠礼缓存租约获取异常 key=%s',
                cache_key,
            )
            db_manager.rollback()
            return None, None
        finally:
            db_manager.remove()

    @staticmethod
    def _release_gift_cache_generation(
        cache_key: str,
        generation_token: str,
    ) -> None:
        """仅释放自己仍持有的 pending 租约；生成成功的缓存行永久保留。"""
        try:
            db_manager.remove()
            row = (
                AdventurerS1AmbientNpcGiftAiCache.query
                .filter_by(cache_key=cache_key)
                .with_for_update()
                .first()
            )
            if (
                row is None
                or row.affinity_effect != _GIFT_CACHE_PENDING_EFFECT
                or row.provider_request_id != generation_token
            ):
                db_manager.rollback()
                return
            AdventurerS1AmbientNpcGiftAiCache.query.filter_by(
                cache_key=cache_key,
                affinity_effect=_GIFT_CACHE_PENDING_EFFECT,
                provider_request_id=generation_token,
            ).delete(synchronize_session=False)
            ok, commit_err = db_manager.commit()
            if not ok:
                current_app.logger.warning(
                    'S1 环境 NPC 赠礼缓存租约释放失败 key=%s: %s',
                    cache_key,
                    commit_err,
                )
                db_manager.rollback()
        except Exception:
            current_app.logger.exception(
                'S1 环境 NPC 赠礼缓存租约释放异常 key=%s',
                cache_key,
            )
            db_manager.rollback()
        finally:
            db_manager.remove()

    @staticmethod
    def _store_gift_cache(
        *,
        cache_key: str,
        template: AmbientNpcTemplate,
        prompt_revision: int,
        tier: str,
        semantic_hash: str,
        semantic: dict,
        normalized: dict,
        ai_call,
        request_id: str,
        generation_token: str,
    ) -> dict | None:
        runtime = dict(getattr(ai_call, 'runtime_metadata', None) or {})
        cache_metadata = dict(getattr(ai_call, 'cache_metadata', None) or {})
        metadata = {**runtime, **cache_metadata}
        ttl_days = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_AI_CACHE_TTL_DAYS',
            180,
            1,
            3650,
        )
        expires_at = _portable_utc_now() + timedelta(days=ttl_days)
        try:
            db_manager.remove()
            row = (
                AdventurerS1AmbientNpcGiftAiCache.query
                .filter_by(cache_key=cache_key)
                .with_for_update()
                .first()
            )
            winner = S1AmbientNpcService._normalized_gift_cache_row(row)
            if winner:
                db_manager.rollback()
                return winner
            if (
                row is None
                or row.affinity_effect != _GIFT_CACHE_PENDING_EFFECT
                or row.provider_request_id != generation_token
            ):
                db_manager.rollback()
                return None
            row.npc_key = template.key
            row.template_revision = template.revision
            row.prompt_revision = int(prompt_revision)
            row.affinity_tier = tier
            row.item_snapshot_hash = semantic_hash
            row.item_semantic_snapshot = dict(semantic)
            row.affinity_effect = normalized['affinity_effect']
            row.response_texts = list(normalized['response_texts'])
            row.model = metadata.get('model')
            row.provider = metadata.get('provider')
            row.profile_id = metadata.get('profile_id')
            row.provider_request_id = (
                metadata.get('request_id') or metadata.get('provider_request_id')
            )
            row.origin_correlation_id = request_id
            row.prompt_hash = metadata.get('prompt_hash')
            row.system_prompt_hash = metadata.get('system_prompt_hash')
            row.contract_hash = metadata.get('contract_hash')
            row.request_hash = metadata.get('request_hash')
            usage = getattr(ai_call, 'usage', None)
            row.usage_snapshot = dict(usage) if isinstance(usage, dict) else None
            row.expires_at = expires_at
            ok, commit_err = db_manager.commit()
            if not ok:
                current_app.logger.warning(
                    'S1 环境 NPC 赠礼 AI 缓存提交失败 key=%s: %s',
                    cache_key,
                    commit_err,
                )
                db_manager.rollback()
                return None
            generated = S1AmbientNpcService._normalized_gift_cache_row(row)
            if generated:
                generated['source'] = 'generated'
                generated['metadata']['cache_hit'] = False
            return generated
        except Exception:
            current_app.logger.exception(
                'S1 环境 NPC 赠礼 AI 缓存写入异常 key=%s',
                cache_key,
            )
            db_manager.rollback()
            return None
        finally:
            db_manager.remove()

    @staticmethod
    def _prepare_gift_ai_result(
        *,
        adventurer_id: int,
        request_id: str,
        surface: str,
        template: AmbientNpcTemplate,
        tier: str,
        semantic: dict,
        semantic_hash: str,
    ) -> dict:
        from plugins.adventurer_s1.services.s1_ambient_npc_gift_ai_service import (
            PROMPT_REVISION,
            build_ambient_npc_gift_ai_cache_metadata,
            call_ambient_npc_gift_ai,
            resolve_ambient_npc_gift_ai_runtime_context,
        )

        runtime_context = resolve_ambient_npc_gift_ai_runtime_context()
        cache_metadata = build_ambient_npc_gift_ai_cache_metadata(
            npc_key=template.key,
            template_revision=template.revision,
            affinity_tier=tier,
            surface_key=surface,
            item_snapshot_hash=semantic_hash,
            item_semantic_snapshot=semantic,
            response_count=8,
            runtime_context=runtime_context,
        )
        cache_key = str(cache_metadata['cache_key'])
        cached = S1AmbientNpcService._load_gift_cache(cache_key)
        if cached:
            return cached
        claimed_cache, generation_token = (
            S1AmbientNpcService._claim_gift_cache_generation(
                cache_key=cache_key,
                template=template,
                prompt_revision=PROMPT_REVISION,
                tier=tier,
                semantic_hash=semantic_hash,
                semantic=semantic,
                request_id=request_id,
            )
        )
        if claimed_cache:
            return claimed_cache
        if not generation_token:
            wait_seconds = S1AmbientNpcService._config_int(
                'ADVENTURER_S1_AMBIENT_NPC_GIFT_AI_CACHE_WAIT_SECONDS',
                6,
                0,
                30,
            )
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(0.1, max(0.01, remaining)))
                cached = S1AmbientNpcService._load_gift_cache(cache_key)
                if cached:
                    return cached
            return S1AmbientNpcService._fallback_gift_result(
                template,
                tier,
                failure_stage='cache_generation_inflight',
                metadata={
                    'cache_key': cache_key,
                    'cache_wait_seconds': wait_seconds,
                    'provider_skipped': True,
                },
            )

        try:
            ai_call = call_ambient_npc_gift_ai(
                request_id=request_id,
                adventurer_id=adventurer_id,
                npc_key=template.key,
                template_revision=template.revision,
                affinity_tier=tier,
                surface_key=surface,
                item_snapshot_hash=semantic_hash,
                item_semantic_snapshot=semantic,
                response_count=8,
                runtime_context=runtime_context,
            )
            normalized = getattr(ai_call, 'normalized', None)
            if getattr(ai_call, 'ok', False) and isinstance(normalized, dict):
                stored = S1AmbientNpcService._store_gift_cache(
                    cache_key=cache_key,
                    template=template,
                    prompt_revision=PROMPT_REVISION,
                    tier=tier,
                    semantic_hash=semantic_hash,
                    semantic=semantic,
                    normalized=normalized,
                    ai_call=ai_call,
                    request_id=request_id,
                    generation_token=generation_token,
                )
                if stored:
                    return stored
                runtime = dict(getattr(ai_call, 'runtime_metadata', None) or {})
                runtime.update(dict(getattr(ai_call, 'cache_metadata', None) or {}))
                runtime['cache_write_failed'] = True
                return {
                    'affinity_effect': normalized['affinity_effect'],
                    'response_texts': list(normalized['response_texts']),
                    'source': 'generated_uncached',
                    'cache_key': cache_key,
                    'failure_stage': 'cache_persistence',
                    'metadata': runtime,
                }
            failure_metadata = dict(getattr(ai_call, 'runtime_metadata', None) or {})
            failure_metadata.update(
                dict(getattr(ai_call, 'cache_metadata', None) or {})
            )
            validation_error = getattr(ai_call, 'validation_error_code', None)
            if validation_error:
                failure_metadata['validation_error_code'] = validation_error
            return S1AmbientNpcService._fallback_gift_result(
                template,
                tier,
                failure_stage=getattr(ai_call, 'failure_stage', None),
                metadata=failure_metadata,
            )
        finally:
            S1AmbientNpcService._release_gift_cache_generation(
                cache_key,
                generation_token,
            )

    @staticmethod
    def _get_or_create_locked_state(adventurer_id: int, npc_key: str):
        state = (
            AdventurerS1AmbientNpcState.query
            .populate_existing()
            .filter_by(adventurer_id=adventurer_id, npc_key=npc_key)
            .with_for_update()
            .first()
        )
        if state:
            return state, None
        state = AdventurerS1AmbientNpcState(
            adventurer_id=adventurer_id,
            npc_key=npc_key,
            affinity_score=0,
            interaction_count=0,
            recent_text_keys={},
        )
        ok, err = db_manager.add(state)
        if ok:
            return state, None
        current_app.logger.error('创建 S1 环境 NPC 状态失败: %s', err)
        return None, '创建环境伙伴关系失败'

    @staticmethod
    def _normalize_action_input(action_input) -> tuple[dict | None, str | None, str | None]:
        if action_input is None:
            action_input = {}
        if not isinstance(action_input, dict):
            return None, None, '互动参数必须是对象'
        try:
            canonical = json.dumps(
                action_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            )
        except (TypeError, ValueError):
            return None, None, '互动参数无法解析'
        if len(canonical.encode('utf-8')) > 4096:
            return None, None, '互动参数过长'
        return dict(action_input), canonical, None

    @staticmethod
    def _request_fingerprint(
        surface_key: str,
        encounter_id: str,
        action_key: str,
        canonical_action_input: str,
    ) -> str:
        raw = (
            f'{surface_key}|{encounter_id}|{action_key}|'
            f'{canonical_action_input}'
        )
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _choose_response(
        state: AdventurerS1AmbientNpcState,
        template: AmbientNpcTemplate,
        action: AmbientNpcActionTemplate,
        tier: str,
    ) -> AmbientNpcTextLine:
        pool = action.responses[tier]
        history_key = f'{action.key}:{tier}'
        history_map = dict(state.recent_text_keys or {})
        recent = [str(key) for key in (history_map.get(history_key) or [])]
        available = [line for line in pool if line.key not in recent]
        line = secrets.choice(available or list(pool))
        limit = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_RECENT_TEXT_LIMIT', 4, 1, 16,
        )
        history_map[history_key] = (recent + [line.key])[-limit:]
        # 顺便清理旧模板留下的无界 key，避免 JSON 随扩展无限增长。
        allowed_prefixes = tuple(f'{key}:' for key in template.actions)
        state.recent_text_keys = {
            key: value
            for key, value in history_map.items()
            if key.startswith(allowed_prefixes)
        }
        return line

    @staticmethod
    def _choose_dynamic_gift_response(
        state: AdventurerS1AmbientNpcState,
        template: AmbientNpcTemplate,
        action: AmbientNpcActionTemplate,
        tier: str,
        response_texts: list[str],
    ) -> AmbientNpcTextLine:
        pool = []
        for text_value in response_texts:
            text_value = str(text_value or '').strip()
            if not text_value:
                continue
            text_hash = hashlib.sha256(text_value.encode('utf-8')).hexdigest()[:20]
            pool.append(AmbientNpcTextLine(f'ai_gift_{text_hash}', text_value))
        if not pool:
            raise ValueError('赠礼回应文案池为空')
        history_key = f'{action.key}:{tier}'
        history_map = dict(state.recent_text_keys or {})
        recent = [str(key) for key in (history_map.get(history_key) or [])]
        available = [line for line in pool if line.key not in recent]
        line = secrets.choice(available or pool)
        limit = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_RECENT_TEXT_LIMIT', 4, 1, 16,
        )
        history_map[history_key] = (recent + [line.key])[-limit:]
        allowed_prefixes = tuple(f'{key}:' for key in template.actions)
        state.recent_text_keys = {
            key: value
            for key, value in history_map.items()
            if key.startswith(allowed_prefixes)
        }
        return line

    @staticmethod
    def _handle_static_text(
        adventurer: AdventurerS1,
        template: AmbientNpcTemplate,
        action: AmbientNpcActionTemplate,
        state: AdventurerS1AmbientNpcState,
        action_input,
    ) -> tuple[dict, str | None]:
        del adventurer, template, action, state, action_input
        return {'affinity_delta': 0, 'item': None}, None

    @staticmethod
    def _handle_consume_gift_ai(
        adventurer: AdventurerS1,
        template: AmbientNpcTemplate,
        action: AmbientNpcActionTemplate,
        state: AdventurerS1AmbientNpcState,
        action_input,
    ) -> tuple[dict | None, str | None]:
        del template, action
        prepared = action_input.get('_prepared_gift')
        if not isinstance(prepared, dict):
            return None, '赠礼状态已失效，请重新选择物品'
        current_tier = S1AmbientNpcService._affinity_tier(
            int(state.affinity_score or 0)
        )
        if current_tier != prepared.get('affinity_tier'):
            return None, '关系状态刚刚发生变化，请重新赠送'
        item, snapshot, semantic, semantic_hash, item_err = (
            S1AmbientNpcService._load_selected_item_snapshot(
                adventurer.id,
                action_input.get('item_ref'),
                for_update=True,
            )
        )
        if item_err:
            return None, item_err
        if semantic_hash != prepared.get('item_snapshot_hash'):
            return None, '这件物品的状态刚刚发生变化，请重新确认赠礼'
        ai_result = prepared.get('ai_result')
        if not isinstance(ai_result, dict):
            return None, '赠礼回应状态已失效，请重试'
        response_texts = ai_result.get('response_texts')
        if not isinstance(response_texts, list) or not response_texts:
            return None, '赠礼回应状态已失效，请重试'
        return {
            'affinity_delta': S1AmbientNpcService._gift_affinity_delta(
                str(ai_result.get('affinity_effect') or '')
            ),
            'item': snapshot,
            'item_row': item,
            'item_ref': {
                'source': snapshot['source'],
                'id': snapshot['id'],
            },
            'item_semantic_snapshot': semantic,
            'item_snapshot_hash': semantic_hash,
            'response_texts': list(response_texts),
            'ai_result': ai_result,
        }, None

    @staticmethod
    def _consume_gift_after_receipt(
        adventurer: AdventurerS1,
        template: AmbientNpcTemplate,
        effect: dict,
        receipt: AdventurerS1AmbientNpcInteraction,
    ) -> str | None:
        _, consume_err = consume_item(
            adventurer.id,
            effect['item_ref'],
            amount=1,
            source_type='ambient_npc_gift',
            description=(
                f'向环境伙伴「{template.name}」赠送'
                f'「{effect["item"]["name"]}」'
            ),
            operation=ITEM_OPERATION_PLAYER_DESTRUCTIVE,
            action_label='向环境伙伴赠礼',
            related_id=str(receipt.id),
            related_type='ambient_npc_interaction',
        )
        if consume_err:
            current_app.logger.warning(
                'S1 环境 NPC 赠礼消耗失败 adventurer_id=%s item_id=%s: %s',
                adventurer.id,
                effect['item']['id'],
                consume_err,
            )
            return '赠礼消耗失败，请重新核对原请求'
        return None

    @staticmethod
    def interact(
        user_id: int,
        *,
        surface_key: str,
        encounter_id: str,
        action_key: str,
        request_id: str,
        action_input=None,
        item_ref=None,
        now_ts: int | float | None = None,
    ) -> tuple[dict | None, str | AmbientNpcInteractionError | None]:
        """异常安全入口；硬崩溃由 TTL 恢复，普通异常立即回滚并释放每日租约。"""
        gift_guard_owner: dict[str, str | None] = {
            'key': None,
            'token': None,
        }
        try:
            return S1AmbientNpcService._interact(
                user_id,
                surface_key=surface_key,
                encounter_id=encounter_id,
                action_key=action_key,
                request_id=request_id,
                action_input=action_input,
                item_ref=item_ref,
                now_ts=now_ts,
                _gift_guard_owner=gift_guard_owner,
            )
        finally:
            guard_key = gift_guard_owner.get('key')
            guard_token = gift_guard_owner.get('token')
            if guard_key and guard_token:
                gift_guard_owner['token'] = None
                db_manager.rollback()
                S1AmbientNpcService._release_gift_cache_generation(
                    guard_key,
                    guard_token,
                )

    @staticmethod
    def _interact(
        user_id: int,
        *,
        surface_key: str,
        encounter_id: str,
        action_key: str,
        request_id: str,
        action_input=None,
        item_ref=None,
        now_ts: int | float | None = None,
        _gift_guard_owner: dict[str, str | None] | None = None,
    ) -> tuple[dict | None, str | AmbientNpcInteractionError | None]:
        gift_guard_owner = _gift_guard_owner or {}
        gift_guard_key: str | None = None
        gift_guard_token: str | None = None

        def _release_gift_guard() -> None:
            nonlocal gift_guard_token
            token = gift_guard_token
            gift_guard_token = None
            gift_guard_owner['token'] = None
            if gift_guard_key and token:
                S1AmbientNpcService._release_gift_cache_generation(
                    gift_guard_key,
                    token,
                )

        def _finish(payload, error):
            _release_gift_guard()
            return payload, error

        if not current_app.config.get('ADVENTURER_S1_AMBIENT_NPC_ENABLED', True):
            return None, '环境伙伴功能暂未开放'
        request_id = str(request_id or '').strip()
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            return None, '无效的互动请求标识'
        surface = S1AmbientNpcService._normalize_surface(surface_key)
        action_key = str(action_key or '').strip()
        if not surface or not _ID_PATTERN.fullmatch(str(encounter_id or '')):
            return None, '无效的 NPC 偶遇参数'

        def _retry_same_request(
            message: str,
            error_code: str,
        ) -> AmbientNpcInteractionError:
            return S1AmbientNpcService._interaction_error(
                message=message,
                error_code=error_code,
                retry_disposition=AMBIENT_NPC_RETRY_SAME_REQUEST,
                request_id=request_id,
                encounter_id=encounter_id,
                action_key=action_key,
                surface=surface,
            )

        if action_input is not None and item_ref is not None:
            return None, '互动参数重复'
        if action_input is None and item_ref is not None:
            action_input = {'item_ref': item_ref}
        if isinstance(action_input, dict) and set(action_input) == {'item_ref'}:
            normalized_item_ref, item_ref_err = S1AmbientNpcService._normalize_item_ref(
                action_input.get('item_ref')
            )
            if item_ref_err:
                return None, item_ref_err
            action_input = {'item_ref': normalized_item_ref}
        action_input, canonical_input, input_err = (
            S1AmbientNpcService._normalize_action_input(action_input)
        )
        if input_err:
            return None, input_err
        request_fingerprint = S1AmbientNpcService._request_fingerprint(
            surface, encounter_id, action_key, canonical_input,
        )

        # 首先只读预检并尽早重放回执。赠礼的缓存/AI 阶段必须发生在任何写锁之外。
        adventurer_id = S1AmbientNpcService._active_owned_adventurer_id(user_id)
        if not adventurer_id:
            return None, '需要拥有存活的自建冒险者'
        request_receipt = (
            AdventurerS1AmbientNpcInteraction.query
            .filter_by(adventurer_id=adventurer_id, request_id=request_id)
            .first()
        )
        if request_receipt:
            if (
                request_receipt.encounter_id != encounter_id
                or request_receipt.surface_key != surface
                or request_receipt.action_key != action_key
                or request_receipt.request_fingerprint != request_fingerprint
            ):
                db_manager.rollback()
                return None, '这个互动请求标识已经用于其他操作'
            result = S1AmbientNpcService._receipt_projection(request_receipt)
            db_manager.rollback()
            return S1AmbientNpcService._interaction_response(
                user_id, surface, result, now_ts=now_ts,
            ), None

        encounter = S1AmbientNpcService._resolve_encounter(
            adventurer_id, surface, encounter_id, now_ts=now_ts,
        )
        if not encounter:
            db_manager.rollback()
            return None, '这次偶遇已经结束，猫猫去了别处'
        template = get_ambient_npc_template(encounter['npc_key'])
        action = template.actions.get(action_key) if template else None
        if not template or not action:
            db_manager.rollback()
            return None, '这个互动选项不存在'
        if action.handler_key == 'consume_gift_ai':
            if set(action_input) != {'item_ref'}:
                db_manager.rollback()
                return None, '赠礼参数只允许指定一件物品'
        elif action.handler_key == 'static_text' and action_input:
            db_manager.rollback()
            return None, '这个互动不接受额外参数'
        prior_receipt = (
            AdventurerS1AmbientNpcInteraction.query
            .filter_by(
                adventurer_id=adventurer_id,
                encounter_id=encounter_id,
                action_key=action_key,
            )
            .first()
        )
        if prior_receipt:
            conflict = S1AmbientNpcService._other_request_completed_error(
                user_id,
                surface,
                encounter_id,
                action_key,
                request_id,
                prior_receipt,
                now_ts=now_ts,
            )
            db_manager.rollback()
            return None, conflict

        prepared_gift = None
        if action.handler_key == 'consume_gift_ai':
            interaction_day = S1AmbientNpcService._interaction_day(now_ts)
            daily_receipt = (
                AdventurerS1AmbientNpcInteraction.query
                .filter_by(
                    adventurer_id=adventurer_id,
                    npc_key=template.key,
                    daily_limit_key=action.daily_limit_key,
                    interaction_day=interaction_day,
                )
                .first()
            )
            if daily_receipt:
                db_manager.rollback()
                return None, '今天已经赠过礼物，明天再来看看吧'
            state_score_row = (
                AdventurerS1AmbientNpcState.query
                .with_entities(AdventurerS1AmbientNpcState.affinity_score)
                .filter_by(adventurer_id=adventurer_id, npc_key=template.key)
                .first()
            )
            affinity_score = int(state_score_row[0] if state_score_row else 0)
            affinity_tier = S1AmbientNpcService._affinity_tier(affinity_score)
            _, item_snapshot, semantic, semantic_hash, item_err = (
                S1AmbientNpcService._load_selected_item_snapshot(
                    adventurer_id,
                    action_input.get('item_ref'),
                    for_update=False,
                )
            )
            if item_err:
                db_manager.rollback()
                return None, item_err
            # 清掉只读事务和 ORM 实例后，才允许统一网关 remove session 并等待外部响应。
            db_manager.remove()
            gift_guard_key = S1AmbientNpcService._daily_gift_generation_cache_key(
                adventurer_id,
                template.key,
                action.daily_limit_key,
                interaction_day,
            )
            guard_cache, gift_guard_token = (
                S1AmbientNpcService._claim_gift_cache_generation(
                    cache_key=gift_guard_key,
                    template=template,
                    prompt_revision=0,
                    tier=affinity_tier,
                    semantic_hash=semantic_hash,
                    semantic=semantic,
                    request_id=request_id,
                )
            )
            if guard_cache or not gift_guard_token:
                return None, _retry_same_request(
                    '这只环境伙伴的赠礼正在处理，请稍后沿用原请求确认结果',
                    'ambient_npc_gift_in_progress',
                )
            gift_guard_owner['key'] = gift_guard_key
            gift_guard_owner['token'] = gift_guard_token
            # B 可能在 A 提交前通过第一次只读预检，却在 A 提交并释放 guard
            # 之后才成功 claim。拿到每日租约后必须再查一次回执，才能让每日上限
            # 同时覆盖 provider 计费，而不只是最终物品消费。
            post_claim_request_receipt = (
                AdventurerS1AmbientNpcInteraction.query
                .filter_by(adventurer_id=adventurer_id, request_id=request_id)
                .first()
            )
            if post_claim_request_receipt:
                if (
                    post_claim_request_receipt.encounter_id != encounter_id
                    or post_claim_request_receipt.surface_key != surface
                    or post_claim_request_receipt.action_key != action_key
                    or post_claim_request_receipt.request_fingerprint
                    != request_fingerprint
                ):
                    db_manager.rollback()
                    return _finish(None, '这个互动请求标识已经用于其他操作')
                result = S1AmbientNpcService._receipt_projection(
                    post_claim_request_receipt
                )
                db_manager.rollback()
                return _finish(
                    S1AmbientNpcService._interaction_response(
                        user_id, surface, result, now_ts=now_ts,
                    ),
                    None,
                )
            post_claim_daily_receipt = (
                AdventurerS1AmbientNpcInteraction.query
                .filter_by(
                    adventurer_id=adventurer_id,
                    npc_key=template.key,
                    daily_limit_key=action.daily_limit_key,
                    interaction_day=interaction_day,
                )
                .first()
            )
            if post_claim_daily_receipt:
                conflict = None
                if (
                    post_claim_daily_receipt.encounter_id == encounter_id
                    and post_claim_daily_receipt.action_key == action_key
                ):
                    conflict = S1AmbientNpcService._other_request_completed_error(
                        user_id,
                        surface,
                        encounter_id,
                        action_key,
                        request_id,
                        post_claim_daily_receipt,
                        now_ts=now_ts,
                    )
                db_manager.rollback()
                if conflict:
                    return _finish(None, conflict)
                return _finish(None, '今天已经赠过礼物，明天再来看看吧')
            db_manager.remove()
            try:
                ai_result = S1AmbientNpcService._prepare_gift_ai_result(
                    adventurer_id=adventurer_id,
                    request_id=request_id,
                    surface=surface,
                    template=template,
                    tier=affinity_tier,
                    semantic=semantic,
                    semantic_hash=semantic_hash,
                )
            except Exception:
                _release_gift_guard()
                raise
            prepared_gift = {
                'interaction_day': interaction_day,
                'affinity_tier': affinity_tier,
                'item_snapshot': item_snapshot,
                'item_snapshot_hash': semantic_hash,
                'ai_result': ai_result,
                'generation_guard_key': gift_guard_key,
                'generation_guard_token': gift_guard_token,
            }
        else:
            db_manager.remove()

        # 最终短事务：冒险者 -> NPC 状态 -> 仓库聚合/物品；所有消费与流水一次提交。
        if not current_app.config.get('ADVENTURER_S1_AMBIENT_NPC_ENABLED', True):
            return _finish(None, '环境伙伴功能暂未开放')
        adventurer = S1AmbientNpcService._lock_active_owned_adventurer(user_id)
        if not adventurer:
            db_manager.rollback()
            return _finish(None, '需要拥有存活的自建冒险者')
        request_receipt = (
            AdventurerS1AmbientNpcInteraction.query
            .filter_by(adventurer_id=adventurer.id, request_id=request_id)
            .first()
        )
        if request_receipt:
            if (
                request_receipt.encounter_id != encounter_id
                or request_receipt.surface_key != surface
                or request_receipt.action_key != action_key
                or request_receipt.request_fingerprint != request_fingerprint
            ):
                db_manager.rollback()
                return _finish(None, '这个互动请求标识已经用于其他操作')
            result = S1AmbientNpcService._receipt_projection(request_receipt)
            db_manager.rollback()
            return _finish(
                S1AmbientNpcService._interaction_response(
                    user_id, surface, result, now_ts=now_ts,
                ),
                None,
            )

        encounter = S1AmbientNpcService._resolve_encounter(
            adventurer.id, surface, encounter_id, now_ts=now_ts,
        )
        if not encounter:
            db_manager.rollback()
            return _finish(None, '这次偶遇已经结束，猫猫去了别处')
        template = get_ambient_npc_template(encounter['npc_key'])
        action = template.actions.get(action_key) if template else None
        if not template or not action:
            db_manager.rollback()
            return _finish(None, '这个互动选项不存在')
        final_interaction_day = None
        if action.daily_limit_key:
            final_interaction_day = S1AmbientNpcService._interaction_day(now_ts)
            if (
                prepared_gift
                and prepared_gift.get('interaction_day') != final_interaction_day
            ):
                db_manager.rollback()
                return _finish(None, '日期刚刚切换，请重新确认这次赠礼')
        if prepared_gift and not S1AmbientNpcService._lock_gift_generation_guard(
            str(prepared_gift.get('generation_guard_key') or ''),
            str(prepared_gift.get('generation_guard_token') or ''),
        ):
            db_manager.rollback()
            return _finish(
                None,
                _retry_same_request(
                    '这次赠礼的处理凭据已过期，请沿用当前礼物重试',
                    'ambient_npc_gift_guard_expired',
                ),
            )
        prior_receipt = (
            AdventurerS1AmbientNpcInteraction.query
            .filter_by(
                adventurer_id=adventurer.id,
                encounter_id=encounter_id,
                action_key=action_key,
            )
            .first()
        )
        if prior_receipt:
            conflict = S1AmbientNpcService._other_request_completed_error(
                user_id,
                surface,
                encounter_id,
                action_key,
                request_id,
                prior_receipt,
                now_ts=now_ts,
            )
            db_manager.rollback()
            return _finish(None, conflict)
        if action.daily_limit_key:
            daily_receipt = (
                AdventurerS1AmbientNpcInteraction.query
                .filter_by(
                    adventurer_id=adventurer.id,
                    npc_key=template.key,
                    daily_limit_key=action.daily_limit_key,
                    interaction_day=final_interaction_day,
                )
                .first()
            )
            if daily_receipt:
                conflict = None
                if (
                    daily_receipt.encounter_id == encounter_id
                    and daily_receipt.action_key == action_key
                ):
                    conflict = S1AmbientNpcService._other_request_completed_error(
                        user_id,
                        surface,
                        encounter_id,
                        action_key,
                        request_id,
                        daily_receipt,
                        now_ts=now_ts,
                    )
                db_manager.rollback()
                if conflict:
                    return _finish(None, conflict)
                return _finish(None, '今天已经赠过礼物，明天再来看看吧')

        state, state_err = S1AmbientNpcService._get_or_create_locked_state(
            adventurer.id, template.key,
        )
        if state_err:
            db_manager.rollback()
            return _finish(
                None,
                _retry_same_request(
                    state_err,
                    'ambient_npc_gift_state_unavailable',
                ) if prepared_gift else state_err,
            )
        handler_name = S1AmbientNpcService._HANDLERS.get(action.handler_key)
        if not handler_name:
            db_manager.rollback()
            return _finish(None, '这个互动暂未开放')
        handler = getattr(S1AmbientNpcService, handler_name)
        handler_input = dict(action_input)
        if action.handler_key == 'consume_gift_ai':
            handler_input['_prepared_gift'] = prepared_gift
        effect, err = handler(adventurer, template, action, state, handler_input)
        if err:
            db_manager.rollback()
            return _finish(None, err)

        requested_affinity_delta = int(effect.get('affinity_delta') or 0)
        affinity_before = int(state.affinity_score or 0)
        state.affinity_score = max(
            -100,
            min(100, affinity_before + requested_affinity_delta),
        )
        affinity_after = state.affinity_score
        affinity_delta = affinity_after - affinity_before
        state.interaction_count = int(state.interaction_count or 0) + 1
        state.last_interaction_at = _portable_utc_now()
        if action.handler_key == 'consume_gift_ai':
            response_tier = prepared_gift['affinity_tier']
            response_line = S1AmbientNpcService._choose_dynamic_gift_response(
                state,
                template,
                action,
                response_tier,
                effect['response_texts'],
            )
        else:
            response_tier = S1AmbientNpcService._affinity_tier(state.affinity_score)
            response_line = S1AmbientNpcService._choose_response(
                state, template, action, response_tier,
            )
        item = effect.get('item')
        ai_result = effect.get('ai_result') if item else None
        ai_metadata = dict((ai_result or {}).get('metadata') or {})
        ai_metadata = json.loads(json.dumps(
            ai_metadata,
            ensure_ascii=False,
            default=str,
        ))
        item_remaining_quantity = (
            max(0, int(item['quantity']) - 1) if item else None
        )
        item_removed = (
            bool(int(item['quantity']) <= 1) if item else None
        )
        receipt = AdventurerS1AmbientNpcInteraction(
            adventurer_id=adventurer.id,
            npc_key=template.key,
            encounter_id=encounter_id,
            surface_key=surface,
            action_key=action.key,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            template_revision=template.revision,
            interaction_day=(
                final_interaction_day if action.daily_limit_key else None
            ),
            daily_limit_key=action.daily_limit_key,
            response_key=response_line.key,
            response_text=response_line.text,
            item_source=item['source'] if item else None,
            item_id=item['id'] if item else None,
            item_name=item['name'] if item else None,
            item_type=item['type'] if item else None,
            item_rarity=item['rarity'] if item else None,
            item_snapshot_hash=(effect.get('item_snapshot_hash') if item else None),
            item_snapshot=(dict(item) if item else None),
            item_remaining_quantity=item_remaining_quantity,
            item_removed=item_removed,
            ai_cache_key=((ai_result or {}).get('cache_key') if item else None),
            ai_result_source=((ai_result or {}).get('source') if item else None),
            ai_affinity_effect=(
                (ai_result or {}).get('affinity_effect') if item else None
            ),
            ai_model=ai_metadata.get('model') if item else None,
            ai_provider=ai_metadata.get('provider') if item else None,
            ai_profile_id=ai_metadata.get('profile_id') if item else None,
            ai_provider_request_id=(
                ai_metadata.get('request_id')
                or ai_metadata.get('provider_request_id')
                if item
                else None
            ),
            ai_failure_stage=((ai_result or {}).get('failure_stage') if item else None),
            ai_response_texts=(
                list((ai_result or {}).get('response_texts') or []) if item else None
            ),
            ai_metadata=ai_metadata if item else None,
            affinity_delta=affinity_delta,
            affinity_before=affinity_before,
            affinity_after=affinity_after,
        )
        added, add_err = db_manager.add(receipt)
        if not added:
            current_app.logger.error('保存 S1 环境 NPC 互动回执失败: %s', add_err)
            db_manager.rollback()
            return _finish(
                None,
                _retry_same_request(
                    '保存互动回执失败',
                    'ambient_npc_gift_receipt_unavailable',
                ) if prepared_gift else '保存互动回执失败',
            )
        if item:
            consume_err = S1AmbientNpcService._consume_gift_after_receipt(
                adventurer,
                template,
                effect,
                receipt,
            )
            if consume_err:
                db_manager.rollback()
                return _finish(
                    None,
                    _retry_same_request(
                        consume_err,
                        'ambient_npc_gift_consume_unavailable',
                    ),
                )
        result = S1AmbientNpcService._receipt_projection(receipt)
        ok, commit_err = db_manager.commit()
        if not ok:
            current_app.logger.error('提交 S1 环境 NPC 互动失败: %s', commit_err)
            db_manager.rollback()
            return _finish(
                None,
                _retry_same_request(
                    '互动失败，请稍后再试',
                    'ambient_npc_gift_commit_unavailable',
                ) if prepared_gift else '互动失败，请稍后再试',
            )
        return _finish(
            S1AmbientNpcService._interaction_response(
                user_id, surface, result, now_ts=now_ts,
            ),
            None,
        )


def cleanup_expired_ambient_npc_gift_ai_cache_job() -> None:
    """按小批量清理过期语义缓存；定时任务必须自行显式提交。"""
    try:
        batch_size = S1AmbientNpcService._config_int(
            'ADVENTURER_S1_AMBIENT_NPC_GIFT_AI_CACHE_CLEANUP_BATCH_SIZE',
            5000,
            100,
            50000,
        )
        cutoff = _portable_utc_now()
        rows = (
            AdventurerS1AmbientNpcGiftAiCache.query
            .with_entities(AdventurerS1AmbientNpcGiftAiCache.cache_key)
            .filter(AdventurerS1AmbientNpcGiftAiCache.expires_at <= cutoff)
            .order_by(AdventurerS1AmbientNpcGiftAiCache.cache_key.asc())
            .limit(batch_size)
            .all()
        )
        cache_keys = [row[0] for row in rows]
        if not cache_keys:
            db_manager.rollback()
            return
        AdventurerS1AmbientNpcGiftAiCache.query.filter(
            AdventurerS1AmbientNpcGiftAiCache.cache_key.in_(cache_keys),
            AdventurerS1AmbientNpcGiftAiCache.expires_at <= cutoff,
        ).delete(synchronize_session=False)
        ok, commit_err = db_manager.commit()
        if not ok:
            current_app.logger.error(
                '清理 S1 环境 NPC 赠礼 AI 缓存失败: %s',
                commit_err,
            )
            db_manager.rollback()
    except Exception:
        current_app.logger.exception('清理 S1 环境 NPC 赠礼 AI 缓存异常')
        db_manager.rollback()
    finally:
        db_manager.remove()
