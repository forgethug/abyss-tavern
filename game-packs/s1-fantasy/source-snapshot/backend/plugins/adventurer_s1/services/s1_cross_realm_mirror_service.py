"""One-shot, read-only Region-1 adventurer cameo for a Region-2 expedition."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import unicodedata
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import select

from db_repository import db_manager
from db_repository.s1_cross_realm_mirror_repository import (
    S1CrossRealmMirrorRepository,
)
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog,
    AdventurerS1Settings,
)
from plugins.adventurer_s1.s1_item_types import (
    S1_INTERNAL_ITEM_TYPE_SET,
    S1_ITEM_TYPE_SET,
)
from plugins.adventurer_s1.services.s1_realm_service import S1RealmService
from plugins.adventurer_s1.services.s1_waiting_player_npc_service import (
    validate_waiting_npc_profile,
)


logger = logging.getLogger(__name__)

REGION_1 = 'region_1'
REGION_2 = 'region_2'
TICKET_SETTING_KEY = 'cross_realm_mirror_ticket'
OFFER_SETTING_PREFIX = 'crm:'
INJECTION_LOG_FIELD = 'cross_realm_mirror_injection'
SNAPSHOT_VERSION = 2
# Existing source assets also use registered internal types; preserve them read-only.
_MIRROR_EQUIPMENT_TYPES = S1_ITEM_TYPE_SET | S1_INTERNAL_ITEM_TYPE_SET
_TICKET_VERSION = 1
_MAX_SKIP_HARD_LIMIT = 32
_MAX_TARGET_DUNGEON_CHARS = 100
_MAX_RESERVED_NAME_HASHES = 12
_EMPTY_INJECTION = {
    'enabled': False,
    'prompt_block': '',
}


class S1CrossRealmMirrorError(RuntimeError):
    """Base error for the one-shot mirror feature."""


class S1CrossRealmMirrorAccessError(S1CrossRealmMirrorError):
    pass


class S1CrossRealmMirrorStateError(S1CrossRealmMirrorError):
    pass


class S1CrossRealmMirrorDeliveryError(S1CrossRealmMirrorStateError):
    """An offered ticket needs the same log's receipt recovered before generation."""


class S1CrossRealmMirrorRequestError(S1CrossRealmMirrorError):
    pass


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace(
        '+00:00', 'Z',
    )


def _is_hex(value: object, length: int) -> bool:
    text = str(value or '')
    return (
        len(text) == length
        and all(char in '0123456789abcdef' for char in text)
    )


def _display_name_hash(value: object) -> str:
    normalized = unicodedata.normalize('NFKC', str(value or '').strip()).casefold()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _contains_reserved_member_name(
    source_name: str,
    reserved_hashes: list[str],
) -> bool:
    """Match exact member names inside the source name without receiving raw names."""
    if not reserved_hashes:
        return False
    normalized = unicodedata.normalize('NFKC', source_name.strip()).casefold()
    reserved = set(reserved_hashes)
    for start in range(len(normalized)):
        for end in range(start + 1, len(normalized) + 1):
            if _display_name_hash(normalized[start:end]) in reserved:
                return True
    return False




def _allowed_user_ids() -> list[int]:
    return []


def _ticket_key(user_id: int) -> str:
    # Preserve the owner's deployed ticket and receipt without a data migration.
    return f'{TICKET_SETTING_KEY}:{int(user_id)}'


def is_cross_realm_mirror_control_visible(user_id: int | None) -> bool:
    """Only configured users on the central Region-1 runtime see the control."""
    return bool(
        user_id is not None
        and int(user_id) in _allowed_user_ids()
        and S1RealmService.current_runtime_realm() == REGION_1
    )


def _require_owner(user_id: int) -> int:
    uid = int(user_id)
    if not is_cross_realm_mirror_control_visible(uid):
        raise S1CrossRealmMirrorAccessError('该入口尚未对当前玩家开放')
    return uid


def _decode_ticket(row: AdventurerS1Settings | None) -> dict | None:
    if row is None or row.value in (None, ''):
        return None
    try:
        state = json.loads(row.value)
    except (TypeError, ValueError) as exc:
        raise S1CrossRealmMirrorStateError('跨区倒影票据状态损坏') from exc
    if not isinstance(state, dict) or state.get('version') != _TICKET_VERSION:
        raise S1CrossRealmMirrorStateError('跨区倒影票据版本无效')
    status = state.get('status')
    if status not in {'pending', 'consumed'}:
        raise S1CrossRealmMirrorStateError('跨区倒影票据状态无效')
    if not _is_hex(state.get('ticket_ref'), 32):
        raise S1CrossRealmMirrorStateError('跨区倒影票据标识无效')
    if status == 'pending':
        remaining = state.get('remaining_skips')
        skipped = state.get('skipped_offer_refs')
        if (
            type(remaining) is not int
            or not 0 <= remaining <= _MAX_SKIP_HARD_LIMIT
            or not isinstance(skipped, list)
            or len(skipped) > _MAX_SKIP_HARD_LIMIT
            or any(not _is_hex(item, 64) for item in skipped)
        ):
            raise S1CrossRealmMirrorStateError('跨区倒影随机落点状态无效')
    if status == 'consumed':
        if not _is_hex(state.get('consumed_offer_ref'), 64):
            raise S1CrossRealmMirrorStateError('跨区倒影消费回执无效')
        _normalize_injection_snapshot(state.get('snapshot'))
    return state


def _write_ticket(
    row: AdventurerS1Settings | None,
    state: dict,
    *,
    owner_user_id: int,
) -> AdventurerS1Settings:
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    if row is None:
        row = AdventurerS1Settings(
            key=_ticket_key(owner_user_id),
            value=encoded,
            description='跨区只读倒影单次票据（内部状态）',
            updated_by_user_id=int(owner_user_id),
        )
        S1CrossRealmMirrorRepository.add_ticket(row)
    else:
        row.value = encoded
        row.description = '跨区只读倒影单次票据（内部状态）'
        row.updated_by_user_id = int(owner_user_id)
    return row


def _source_membership(user_id: int):
    membership = S1CrossRealmMirrorRepository.get_membership(int(user_id))
    if (
        membership is None
        or membership.realm_code != REGION_1
        or membership.slot_state != 'active'
        or not membership.active_adventurer_id
    ):
        return None
    return membership


def _source_name(user_id: int) -> str | None:
    membership = _source_membership(user_id)
    if membership is None:
        return None
    return S1CrossRealmMirrorRepository.get_active_source_name(
        adventurer_id=int(membership.active_adventurer_id),
        owner_user_id=int(user_id),
        realm_code=REGION_1,
    )


def _clip_text(value: object, limit: int) -> str:
    return str(value or '').strip()[:limit]


def _build_source_snapshot(user_id: int, *, ticket_ref: str) -> dict | None:
    membership = _source_membership(user_id)
    if membership is None:
        return None
    adventurer = S1CrossRealmMirrorRepository.get_active_source_adventurer(
        adventurer_id=int(membership.active_adventurer_id),
        owner_user_id=int(user_id),
        realm_code=REGION_1,
    )
    if adventurer is None:
        return None

    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        build_expedition_equipment_context,
    )
    from plugins.adventurer_s1.services.s1_waiting_player_npc_service import (
        _profile_desc_max_chars,
        _profile_limit,
    )

    item_dict, _ = build_expedition_equipment_context([adventurer])
    desc_limit = _profile_desc_max_chars()

    equipments = []
    for equipment in sorted(adventurer.equipment_items, key=lambda row: int(row.id or 0)):
        if len(equipments) >= _profile_limit('equipment'):
            break
        equipments.append({
            'name': _clip_text(equipment.name, 100),
            'type': equipment.type,
            'rarity': equipment.rarity,
            'desc': _clip_text(
                equipment.custom_desc or item_dict.get(equipment.name, ''),
                desc_limit,
            ),
            'quantity': max(1, min(int(equipment.quantity or 1), 999)),
        })

    skills = []
    for skill in sorted(adventurer.skill_items, key=lambda row: int(row.id or 0)):
        if not skill.equipped:
            continue
        if len(skills) >= _profile_limit('skill'):
            break
        skills.append({
            'name': _clip_text(skill.name, 100),
            'desc': _clip_text(skill.description, desc_limit),
        })

    traits = []
    for trait in sorted(adventurer.traits, key=lambda row: int(row.id or 0)):
        if trait.is_archived:
            continue
        if len(traits) >= _profile_limit('trait'):
            break
        traits.append({
            'name': _clip_text(trait.name, 100),
            'desc': _clip_text(trait.description, desc_limit),
        })

    profile, profile_errors = validate_waiting_npc_profile({
        'character_class': (
            _clip_text(adventurer.character_class, 50) or None
        ),
        'equipments': equipments,
        'skills': skills,
        'traits': traits,
    }, path='cross_realm_mirror.profile', allowed_equipment_types=_MIRROR_EQUIPMENT_TYPES)
    if profile_errors:
        raise S1CrossRealmMirrorStateError(
            '一区冒险者倒影资料不满足有界投影：'
            + '；'.join(profile_errors[:5])
        )

    snapshot = {
        'version': SNAPSHOT_VERSION,
        'ticket_ref': str(ticket_ref),
        'snapshot_at': _utc_iso_now(),
        'candidate': {
            'display_name': _clip_text(adventurer.name, 100),
            'gender': _clip_text(adventurer.gender, 10) or '未知',
            'personality': [
                _clip_text(part, 40)
                for part in str(adventurer.personality or '').split(',')
                if _clip_text(part, 40)
            ][:8],
            'level': max(1, int(adventurer.level or 1)),
            'profile': profile,
        },
    }
    return _normalize_injection_snapshot(snapshot)


def _normalize_injection_snapshot(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or type(value.get('version')) is not int
        or value['version'] not in {1, SNAPSHOT_VERSION}
    ):
        raise S1CrossRealmMirrorStateError('跨区倒影快照版本无效')
    if not _is_hex(value.get('ticket_ref'), 32):
        raise S1CrossRealmMirrorStateError('跨区倒影快照标识无效')
    snapshot_at = value.get('snapshot_at')
    if not isinstance(snapshot_at, str) or not 1 <= len(snapshot_at) <= 40:
        raise S1CrossRealmMirrorStateError('跨区倒影快照时间无效')
    candidate = value.get('candidate')
    if not isinstance(candidate, dict):
        raise S1CrossRealmMirrorStateError('跨区倒影候选无效')
    display_name = candidate.get('display_name')
    gender = candidate.get('gender')
    personality = candidate.get('personality')
    level = candidate.get('level')
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name.strip()) > 100
        or not isinstance(gender, str)
        or not gender.strip()
        or len(gender.strip()) > 10
        or not isinstance(personality, list)
        or len(personality) > 8
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > 40
            for item in personality
        )
        or type(level) is not int
        or level < 1
    ):
        raise S1CrossRealmMirrorStateError('跨区倒影候选字段无效')
    profile, profile_errors = validate_waiting_npc_profile(
        candidate.get('profile'),
        path='cross_realm_mirror.candidate.profile',
        allowed_equipment_types=_MIRROR_EQUIPMENT_TYPES,
    )
    if profile_errors:
        raise S1CrossRealmMirrorStateError(
            '跨区倒影候选档案无效：' + '；'.join(profile_errors[:5])
        )
    return {
        'enabled': True,
        'version': value['version'],
        'appearance_required': value['version'] == SNAPSHOT_VERSION,
        'ticket_ref': str(value['ticket_ref']),
        'snapshot_at': snapshot_at,
        'candidate': {
            'display_name': display_name.strip(),
            'gender': gender.strip(),
            'personality': [item.strip() for item in personality],
            'level': level,
            'profile': profile,
        },
    }


def _render_prompt_block(snapshot: dict) -> str:
    if snapshot.get('enabled') is not True:
        return ''
    candidate = snapshot.get('candidate')
    if not isinstance(candidate, dict):
        return ''
    candidate_json = json.dumps(
        {
            'appearance_required': snapshot.get('appearance_required') is True,
            'candidate': candidate,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return (
        '【本场远方旅人倒影（系统专项契约的冻结输入）】\n'
        f'{candidate_json}\n'
        '- 本场出场要求与人物资料均已冻结，按 system 中的'
        '【深渊酒馆·远方旅人倒影】完成。'
    )


def _public_status_from_state(user_id: int, state: dict | None) -> dict:
    source_name = _source_name(user_id)
    pending = bool(state and state.get('status') == 'pending')
    return {
        'visible': True,
        'state': 'pending' if pending else 'ready',
        'source_ready': bool(source_name),
        'source_adventurer_name': source_name,
        'armed_at': state.get('armed_at') if pending else None,
        'last_consumed_at': (
            state.get('consumed_at')
            if state and state.get('status') == 'consumed'
            else state.get('last_consumed_at') if state else None
        ),
    }


def get_cross_realm_mirror_status(user_id: int) -> dict:
    uid = _require_owner(user_id)
    state = _decode_ticket(
        S1CrossRealmMirrorRepository.get_ticket(_ticket_key(uid))
    )
    return _public_status_from_state(uid, state)


def arm_cross_realm_mirror(user_id: int) -> dict:
    raise S1CrossRealmMirrorStateError('自建酒馆不启用官方跨区倒影')


def _offer_ref(
    *,
    runtime_id: str,
    deployment_id: str,
    expedition_log_id: int,
) -> str:
    material = (
        f'{deployment_id}\n{runtime_id}\n{int(expedition_log_id)}'
    ).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def _normalize_reserved_name_hashes(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_RESERVED_NAME_HASHES:
        raise S1CrossRealmMirrorRequestError('二区副本成员名称指纹无效')
    if any(not _is_hex(item, 64) for item in value):
        raise S1CrossRealmMirrorRequestError('二区副本成员名称指纹无效')
    return list(dict.fromkeys(str(item) for item in value))


def _offer_setting_key(offer_ref: str) -> str:
    # A full SHA-256 in base64url fits the existing VARCHAR(50) setting key.
    encoded = base64.urlsafe_b64encode(bytes.fromhex(offer_ref)).decode('ascii').rstrip('=')
    return f'{OFFER_SETTING_PREFIX}{encoded}'


def _read_offer_result(offer_ref: str) -> dict | None:
    row = S1CrossRealmMirrorRepository.get_ticket(
        _offer_setting_key(offer_ref), for_update=True,
    )
    if row is None:
        return None
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError) as exc:
        raise S1CrossRealmMirrorStateError('跨区倒影领取回执损坏') from exc
    if (
        not isinstance(value, dict)
        or type(value.get('version')) is not int
        or value['version'] != 1
        or type(value.get('injected')) is not bool
    ):
        raise S1CrossRealmMirrorStateError('跨区倒影领取回执无效')
    if not value['injected']:
        return {'injected': False}
    return {
        'injected': True,
        'snapshot': _normalize_injection_snapshot(value.get('snapshot')),
    }


def _save_offer_result(offer_ref: str, result: dict, *, owner_user_id: int) -> dict:
    # Called under the shared owner lock; each Region-2 log has one immutable result.
    S1CrossRealmMirrorRepository.add_ticket(AdventurerS1Settings(
        key=_offer_setting_key(offer_ref),
        value=json.dumps({'version': 1, **result}, ensure_ascii=False),
        description='跨区只读倒影副本领取回执（内部状态）',
        updated_by_user_id=owner_user_id,
    ))
    return result


def _save_offer_receipt(state: dict, *, owner_user_id: int) -> dict:
    offer_ref = str(state['consumed_offer_ref'])
    existing = _read_offer_result(offer_ref)
    if existing is not None:
        if (
            not existing['injected']
            or existing['snapshot']['ticket_ref'] != state['ticket_ref']
        ):
            raise S1CrossRealmMirrorStateError('跨区倒影领取回执与票据不一致')
        return existing
    return _save_offer_result(offer_ref, {
        'injected': True,
        'snapshot': _normalize_injection_snapshot(state['snapshot']),
    }, owner_user_id=owner_user_id)


def offer_cross_realm_mirror_from_region_2(
    *,
    runtime_id: str,
    deployment_id: str,
    expedition_log_id: int,
    target_dungeon: str,
    reserved_name_hashes: list[str] | None = None,
) -> dict:
    raise S1CrossRealmMirrorStateError('自建酒馆不启用官方跨区倒影')


def _stored_injection(expedition_log_id: int, *, for_update: bool) -> tuple[
    AdventurerS1ExpeditionLog | None,
    dict | None,
]:
    query = AdventurerS1ExpeditionLog.query.filter_by(
        id=int(expedition_log_id),
    )
    if for_update:
        query = query.with_for_update()
    log = query.first()
    if log is None:
        return None, None
    full_log = log.full_log if isinstance(log.full_log, dict) else {}
    value = full_log.get(INJECTION_LOG_FIELD)
    return log, dict(value) if isinstance(value, dict) else None


def _reserved_member_name_hashes(log: AdventurerS1ExpeditionLog) -> list[str]:
    full_log = log.full_log if isinstance(log.full_log, dict) else {}
    rows = full_log.get('audit_member_snapshot')
    if not isinstance(rows, list):
        return []
    result: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get('adventurer_name') or '').strip()
        if not name:
            continue
        name_hash = _display_name_hash(name)
        if name_hash not in result:
            result.append(name_hash)
        if len(result) >= _MAX_RESERVED_NAME_HASHES:
            break
    return result


def prepare_cross_realm_mirror_injection(
    expedition_log_id: int | None,
    target_dungeon: str,
    *,
    eligible: bool,
) -> dict:
    """Region-2 only: offer once outside a DB transaction, then freeze locally."""
    if (
        not expedition_log_id
        or not eligible
        or S1RealmService.current_runtime_realm() != REGION_2
    ):
        return dict(_EMPTY_INJECTION)
    try:
        log, frozen = _stored_injection(int(expedition_log_id), for_update=False)
        if log is None or log.status != 'generating':
            return dict(_EMPTY_INJECTION)
        if frozen is not None:
            result = dict(frozen)
            result['prompt_block'] = _render_prompt_block(result)
            return result
        reserved_name_hashes = _reserved_member_name_hashes(log)
    finally:
        # Frozen/terminal/error exits must release the read too, not only the relay path.
        db_manager.remove()
    client = current_app.extensions.get('s1_active_slot_client')
    if client is None or not hasattr(client, 'offer_cross_realm_mirror'):
        logger.warning('Region-2 cross-realm mirror relay is not ready')
        return dict(_EMPTY_INJECTION)
    try:
        offered = client.offer_cross_realm_mirror(
            expedition_log_id=int(expedition_log_id),
            target_dungeon=str(target_dungeon),
            reserved_name_hashes=reserved_name_hashes,
        )
        if not isinstance(offered, dict) or type(offered.get('injected')) is not bool:
            raise S1CrossRealmMirrorStateError('跨区倒影中继回执无效')
        stored = (
            _normalize_injection_snapshot(offered.get('snapshot'))
            if offered['injected'] else {'enabled': False}
        )
    except Exception as exc:
        logger.warning(
            'Region-2 cross-realm mirror offer failed; expedition_log_id=%s',
            expedition_log_id,
            exc_info=True,
        )
        db_manager.remove()
        # The central runtime may already have consumed a ticket. Retrying this log
        # replays its fixed receipt; proceeding without it could lose the appearance.
        raise S1CrossRealmMirrorDeliveryError('跨区倒影领取回执暂未确认') from exc

    try:
        with db_manager.transaction():
            locked_log, existing = _stored_injection(
                int(expedition_log_id),
                for_update=True,
            )
            if locked_log is None or locked_log.status != 'generating':
                return dict(_EMPTY_INJECTION)
            if existing is None:
                full_log = (
                    dict(locked_log.full_log)
                    if isinstance(locked_log.full_log, dict) else {}
                )
                full_log[INJECTION_LOG_FIELD] = stored
                locked_log.full_log = full_log
                existing = stored
            result = dict(existing)
            result['prompt_block'] = _render_prompt_block(result)
            return result
    except Exception as exc:
        logger.exception(
            'Region-2 cross-realm mirror freeze failed; expedition_log_id=%s',
            expedition_log_id,
        )
        raise S1CrossRealmMirrorDeliveryError('跨区倒影领取回执暂未保存') from exc


def load_cross_realm_mirror_injection(
    expedition_log_id: int | None,
) -> dict:
    """Return a frozen value without carrying a read transaction into prompt assembly."""
    if not expedition_log_id:
        return dict(_EMPTY_INJECTION)
    # The next prompt phase scans history on independent connections. Release the
    # caller's completed reads and this snapshot before that scan or local rendering.
    db_manager.remove()
    with db_manager.readonly_snapshot() as connection:
        result, _ = db_manager.execute_statement(
            select(AdventurerS1ExpeditionLog.full_log).where(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
            ),
            connection=connection,
            raise_errors=True,
        )
        full_log = result.scalar_one_or_none()
        frozen = full_log.get(INJECTION_LOG_FIELD) if isinstance(full_log, dict) else None
    if not isinstance(frozen, dict):
        return dict(_EMPTY_INJECTION)
    result = dict(frozen)
    result['prompt_block'] = _render_prompt_block(result)
    return result
