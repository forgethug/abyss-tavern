"""Operator workflow for starting a shared S1 world-campaign spine.

The workflow creates one non-runtime spine and ordinary world-story fronts in
one transaction.  Front progression, revisions, faction links and narrative
leases remain owned by the existing world-outline machinery.
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import json
import re
from typing import Any

from flask import current_app
from sqlalchemy.exc import IntegrityError

from db_repository import DatabaseError, TransactionError, db_manager
from db_repository.s1_world_campaign_repository import (
    S1WorldCampaignRepository,
)
from plugins.adventurer_s1.models import (
    AdventurerS1FactionProfile,
    AdventurerS1FactionProfileThread,
    AdventurerS1WorldStoryThread,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEONS,
)
from plugins.adventurer_s1.services.s1_world_outline_contract import (
    FACTION_PROFILE_STATUSES,
)
from plugins.adventurer_s1.services.s1_world_outline_service import (
    ABYSS_TAVERN_FACTION_SCOPE_KEY,
    WORLD_INTEL_ALL_DUNGEONS_SCOPE,
    WORLD_INTEL_STATUSES,
    WORLD_INTEL_THREAD_TYPES,
)


CAMPAIGN_ROLES = ('spine', 'faction_front', 'dungeon_front')
CAMPAIGN_FRONT_ROLES = ('faction_front', 'dungeon_front')
CAMPAIGN_SCHEMA_VERSION = 1
CAMPAIGN_SNAPSHOT_OPERATION = 'world_campaign_create_v1'

_CAMPAIGN_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{2,63}$')
_DUNGEON_FRONT_THREAD_TYPES = tuple(
    value for value in WORLD_INTEL_THREAD_TYPES if value != 'faction'
)
_THREAD_RESPONSE_FIELDS = (
    'id', 'title', 'thread_type', 'status', 'base_dungeon', 'scope_keys',
    'trigger_keywords', 'summary', 'next_reveal_hint', 'power_tier',
    'power_profile', 'weight', 'revision', 'chain_key', 'predecessor_id',
    'superseded_by_id', 'is_chain_head', 'campaign_key',
    'campaign_root_id', 'campaign_role', 'campaign_agenda', 'created_at',
    'updated_at',
)


class CampaignInputError(ValueError):
    """A syntactically invalid operator campaign specification."""


def _configured_integer(key: str, *, minimum: int, maximum: int) -> int:
    raw = current_app.config.get(key)
    if raw is None or isinstance(raw, bool):
        raise RuntimeError(f'缺少或无效的 S1 共同战役配置：{key}')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f'无效的 S1 共同战役整数配置：{key}') from None
    if value < minimum or value > maximum:
        raise RuntimeError(
            f'S1 共同战役配置超出范围：{key}={value}，'
            f'允许 {minimum}-{maximum}'
        )
    return value


def _max_fronts() -> int:
    return _configured_integer(
        'ADVENTURER_S1_WORLD_CAMPAIGN_MAX_FRONTS',
        minimum=1,
        maximum=50,
    )


def _max_active_teams() -> int:
    return _configured_integer('ADVENTURER_S1_CAMPAIGN_MAX_ACTIVE_TEAMS', minimum=1, maximum=50)


def _max_profiles_per_front() -> int:
    return _configured_integer(
        'ADVENTURER_S1_WORLD_CAMPAIGN_MAX_PROFILES_PER_FRONT',
        minimum=1,
        maximum=20,
    )


def _default_thread_weight() -> int:
    return _configured_integer(
        'ADVENTURER_S1_WORLD_CAMPAIGN_DEFAULT_WEIGHT',
        minimum=1,
        maximum=100,
    )


def _required_text(value: object, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise CampaignInputError(f'{field} 必须是字符串')
    result = value.strip()
    if not result:
        raise CampaignInputError(f'{field} 不能为空')
    if len(result) > limit:
        raise CampaignInputError(f'{field} 最多 {limit} 个字符')
    return result


def _optional_text(value: object, field: str, *, limit: int) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise CampaignInputError(f'{field} 必须是字符串')
    result = value.strip()
    if len(result) > limit:
        raise CampaignInputError(f'{field} 最多 {limit} 个字符')
    return result


def _bounded_integer(
    value: object,
    field: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignInputError(f'{field} 必须是整数')
    if value < minimum or value > maximum:
        raise CampaignInputError(f'{field} 必须在 {minimum} 到 {maximum} 之间')
    return value


def _text_list(
    value: object,
    field: str,
    *,
    limit: int,
    item_limit: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CampaignInputError(f'{field} 必须是数组')
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        text = _required_text(raw, f'{field}[{index}]', limit=item_limit)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    if len(result) > limit:
        raise CampaignInputError(f'{field} 最多 {limit} 项')
    return result


def _positive_id_list(
    value: object,
    field: str,
    *,
    limit: int,
) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CampaignInputError(f'{field} 必须是数组')
    result: list[int] = []
    for index, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise CampaignInputError(f'{field}[{index}] 必须是正整数')
        if raw not in result:
            result.append(raw)
    if len(result) > limit:
        raise CampaignInputError(f'{field} 最多 {limit} 项')
    return sorted(result)


def _canonical_hash(spec: dict) -> str:
    payload = json.dumps(
        spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _facet_summary(profile: AdventurerS1FactionProfile, key: str) -> str:
    facet = (profile.facets or {}).get(key)
    if isinstance(facet, dict):
        value = facet.get('summary')
        return value.strip() if isinstance(value, str) else ''
    return facet.strip() if isinstance(facet, str) else ''


def _serialize_profile_option(profile: AdventurerS1FactionProfile) -> dict:
    return {
        'id': int(profile.id),
        'faction_key': profile.faction_key or '',
        'canonical_name': profile.canonical_name or '',
        'status': profile.status or '',
        'identity_summary': profile.identity_summary or '',
        'importance': int(profile.importance or 50),
        'revision': int(profile.revision or 1),
        'public_agenda': _facet_summary(profile, 'current_agenda'),
        'current_state': _facet_summary(profile, 'current_state'),
    }


def _serialize_thread(row: AdventurerS1WorldStoryThread) -> dict:
    raw = row.to_admin_dict()
    return {field: raw.get(field) for field in _THREAD_RESPONSE_FIELDS}


def _normalize_thread_payload(
    raw: dict,
    *,
    path: str,
    role: str,
    activity_dungeons: set[str] | None = None,
) -> dict:
    title = _required_text(raw.get('title'), f'{path}.title', limit=120)
    summary = _required_text(raw.get('summary'), f'{path}.summary', limit=8000)
    agenda = _required_text(
        raw.get('campaign_agenda', raw.get('agenda')),
        f'{path}.campaign_agenda',
        limit=4000,
    )
    if role == 'spine':
        return {
            'title': title,
            'thread_type': 'event',
            'base_dungeon': WORLD_INTEL_ALL_DUNGEONS_SCOPE,
            'scope_keys': [WORLD_INTEL_ALL_DUNGEONS_SCOPE],
            'trigger_keywords': _text_list(
                raw.get('trigger_keywords'),
                f'{path}.trigger_keywords',
                limit=10,
                item_limit=50,
            ),
            'summary': summary,
            'next_reveal_hint': _optional_text(
                raw.get('next_reveal_hint'),
                f'{path}.next_reveal_hint',
                limit=4000,
            ),
            'power_tier': _optional_text(
                raw.get('power_tier'), f'{path}.power_tier', limit=30,
            ),
            'power_profile': _optional_text(
                raw.get('power_profile'),
                f'{path}.power_profile',
                limit=4000,
            ),
            'weight': _bounded_integer(
                raw.get('weight'), f'{path}.weight',
                default=_default_thread_weight(), minimum=1, maximum=100,
            ),
            'campaign_agenda': agenda,
        }

    base_dungeon = _required_text(
        raw.get('base_dungeon'), f'{path}.base_dungeon', limit=100,
    )
    if base_dungeon == WORLD_INTEL_ALL_DUNGEONS_SCOPE:
        raise CampaignInputError(f'{path}.base_dungeon 必须是准确地牢，不能是全部地牢')
    valid_dungeons = activity_dungeons if activity_dungeons is not None else {
        str(item.get('name') or '').strip()
        for item in S1_ACTIVE_DUNGEONS
        if str(item.get('name') or '').strip()
    }
    if base_dungeon not in valid_dungeons:
        raise CampaignInputError(f'{path}.base_dungeon 不是当前 S1 权威地牢')
    scope_keys = _text_list(
        raw.get('scope_keys'), f'{path}.scope_keys', limit=8, item_limit=80,
    )
    if WORLD_INTEL_ALL_DUNGEONS_SCOPE in scope_keys:
        raise CampaignInputError(f'{path}.scope_keys 不能包含全部地牢')
    tavern_interaction = raw.get('tavern_interaction') is True
    if tavern_interaction and role != 'faction_front':
        raise CampaignInputError(
            f'{path}.tavern_interaction 仅允许用于 faction_front'
        )
    remaining_scopes = sorted({
        value for value in scope_keys
        if value not in {base_dungeon, ABYSS_TAVERN_FACTION_SCOPE_KEY}
    })
    normalized_scopes = [base_dungeon, *remaining_scopes]
    if tavern_interaction:
        normalized_scopes.append(ABYSS_TAVERN_FACTION_SCOPE_KEY)
    if len(normalized_scopes) > 8:
        raise CampaignInputError(f'{path}.scope_keys 加入权威地牢范围后最多 8 项')

    profile_ids = _positive_id_list(
        raw.get('faction_profile_ids'),
        f'{path}.faction_profile_ids',
        limit=_max_profiles_per_front(),
    )
    if activity_dungeons is not None:
        if role != 'dungeon_front' or profile_ids or tavern_interaction:
            raise CampaignInputError(f'{path} 独立活动只允许新地图前线，不绑定既有势力或酒馆')
        if not set(normalized_scopes).issubset(activity_dungeons):
            raise CampaignInputError(f'{path}.scope_keys 只能包含本活动地图')
    if role == 'faction_front' and len(profile_ids) != 1:
        raise CampaignInputError(
            f'{path}.faction_profile_ids 对 faction_front 必须且只能有 1 项'
        )
    raw_thread_type = raw.get('thread_type')
    if role == 'faction_front':
        if raw_thread_type is not None and raw_thread_type != 'faction':
            raise CampaignInputError(f'{path}.thread_type 必须是 faction')
        thread_type = 'faction'
    else:
        thread_type = raw_thread_type or 'event'
        if thread_type not in _DUNGEON_FRONT_THREAD_TYPES:
            raise CampaignInputError(
                f'{path}.thread_type 必须是 {list(_DUNGEON_FRONT_THREAD_TYPES)} 之一'
            )
    return {
        'role': role,
        'title': title,
        'thread_type': thread_type,
        'base_dungeon': base_dungeon,
        'scope_keys': normalized_scopes,
        'trigger_keywords': sorted(_text_list(
            raw.get('trigger_keywords'),
            f'{path}.trigger_keywords',
            limit=10,
            item_limit=50,
        )),
        'summary': summary,
        'next_reveal_hint': _optional_text(
            raw.get('next_reveal_hint'),
            f'{path}.next_reveal_hint',
            limit=4000,
        ),
        'power_tier': _optional_text(
            raw.get('power_tier'), f'{path}.power_tier', limit=30,
        ),
        'power_profile': _optional_text(
            raw.get('power_profile'), f'{path}.power_profile', limit=4000,
        ),
        'weight': _bounded_integer(
            raw.get('weight'), f'{path}.weight',
            default=_default_thread_weight(), minimum=1, maximum=100,
        ),
        'campaign_agenda': agenda,
        'faction_profile_ids': profile_ids,
        'tavern_interaction': tavern_interaction,
    }


def _normalize_spec(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise CampaignInputError('请求体必须是对象')
    dispatch_enabled = payload.get('dispatch_enabled', False)
    if type(dispatch_enabled) is not bool:
        raise CampaignInputError('dispatch_enabled 必须是布尔值')
    isolated_world = payload.get('isolated_world')
    activity_dungeons = None
    if isolated_world is not None:
        if not dispatch_enabled or not isinstance(isolated_world, dict):
            raise CampaignInputError('独立活动世界需要启用议事厅共同任务并提供地图目录')
        entries = isolated_world.get('dungeons')
        if not isinstance(entries, list) or not 1 <= len(entries) <= _max_fronts():
            raise CampaignInputError(f'isolated_world.dungeons 需要 1-{_max_fronts()} 个新地图')
        from .adventurer_s1_config_service import S1_DUNGEONS, S1_SPECIAL_DUNGEON_MAP, S1_HIDDEN_DUNGEON_BY_NAME
        reserved = {item['name'] for item in S1_DUNGEONS} | set(S1_SPECIAL_DUNGEON_MAP) | set(S1_HIDDEN_DUNGEON_BY_NAME)
        tiers = {(item['min_level'], item['max_level']) for item in S1_ACTIVE_DUNGEONS}
        normalized = []
        for index, entry in enumerate(entries):
            path = f'isolated_world.dungeons[{index}]'
            if not isinstance(entry, dict):
                raise CampaignInputError(f'{path} 必须为对象')
            name = _required_text(entry.get('name'), f'{path}.name', limit=80)
            if (name in reserved or name.startswith(('__', 's1_location_state:', 's1_expedition_locus:'))
                    or name == ABYSS_TAVERN_FACTION_SCOPE_KEY):
                raise CampaignInputError(f'{path}.name 必须是全新的活动地图名')
            minimum, maximum = entry.get('min_level'), entry.get('max_level')
            if type(minimum) is not int or type(maximum) is not int or (minimum, maximum) not in tiers:
                raise CampaignInputError(f'{path} 难度须选用当前开放的等级档位')
            item = {'name': name, 'min_level': minimum, 'max_level': maximum,
                    'setting': _required_text(entry.get('setting'), f'{path}.setting', limit=4000)}
            if entry.get('district') is not None:
                item['district'] = _required_text(entry['district'], f'{path}.district', limit=80)
            if entry.get('challenge') is not None:
                from .s1_campaign_challenge_contract import normalize_challenge
                try:
                    item['challenge'] = normalize_challenge(entry['challenge'])
                except ValueError as error:
                    raise CampaignInputError(f'{path}.challenge: {error}') from error
            normalized.append(item)
        activity_dungeons = {entry['name'] for entry in normalized}
        if len(activity_dungeons) != len(normalized):
            raise CampaignInputError('活动地图名不能重复')
        capacity = isolated_world.get('max_active_teams', 1)
        if type(capacity) is not int or not 1 <= capacity <= min(_max_active_teams(), len(normalized)):
            raise CampaignInputError('同时行动小队数必须为整数，且不超过现场数与活动容量上限')
        isolated_world = {'dungeons': sorted(normalized, key=lambda entry: entry['name']),
                          'max_active_teams': capacity}
        if capacity > 1 and payload.get('stage_plan') is None:
            raise CampaignInputError('多队同时行动需要阶段计划，为各现场保存独立任务与进度')
    campaign_key = _required_text(
        payload.get('campaign_key'), 'campaign_key', limit=64,
    ).lower()
    if not _CAMPAIGN_KEY_RE.fullmatch(campaign_key):
        raise CampaignInputError(
            'campaign_key 只能使用小写字母、数字、点、下划线或连字符，长度 3-64'
        )
    spine_raw = payload.get('spine')
    if not isinstance(spine_raw, dict):
        raise CampaignInputError('spine 必须是对象')
    fronts_raw = payload.get('fronts')
    if not isinstance(fronts_raw, list):
        raise CampaignInputError('fronts 必须是数组')
    if not fronts_raw:
        raise CampaignInputError('fronts 至少需要 1 项')
    max_fronts = _max_fronts()
    if len(fronts_raw) > max_fronts:
        raise CampaignInputError(f'fronts 最多 {max_fronts} 项')

    fronts: list[dict] = []
    title_keys = {
        _required_text(spine_raw.get('title'), 'spine.title', limit=120).casefold()
    }
    signatures: set[tuple] = set()
    faction_front_profile_ids: set[int] = set()
    for index, raw in enumerate(fronts_raw):
        path = f'fronts[{index}]'
        if not isinstance(raw, dict):
            raise CampaignInputError(f'{path} 必须是对象')
        role = raw.get('role')
        if role not in CAMPAIGN_FRONT_ROLES:
            raise CampaignInputError(
                f'{path}.role 必须是 {list(CAMPAIGN_FRONT_ROLES)} 之一'
            )
        front = _normalize_thread_payload(raw, path=path, role=role, activity_dungeons=activity_dungeons)
        title_key = front['title'].casefold()
        if title_key in title_keys:
            raise CampaignInputError(f'{path}.title 在本次战役中重复')
        title_keys.add(title_key)
        if role == 'faction_front':
            profile_id = int(front['faction_profile_ids'][0])
            signature = (role, front['base_dungeon'], profile_id)
            if profile_id in faction_front_profile_ids:
                raise CampaignInputError(
                    f'{path} 与其它 faction_front 重复绑定势力 {profile_id}'
                )
            faction_front_profile_ids.add(profile_id)
        else:
            signature = (role, front['base_dungeon'])
        if signature in signatures:
            raise CampaignInputError(f'{path} 与其它前线归属重复')
        signatures.add(signature)
        fronts.append(front)
    front_roles = {item['role'] for item in fronts}
    if ('dungeon_front' not in front_roles
            or (not dispatch_enabled and 'faction_front' not in front_roles)):
        raise CampaignInputError(
            '议事厅事件至少需要 1 条 dungeon_front；普通共同战役还需要 1 条 faction_front'
        )
    fronts.sort(key=lambda item: (
        item['role'], item['base_dungeon'], item['title'].casefold(),
    ))
    spec = {
        'schema_version': CAMPAIGN_SCHEMA_VERSION,
        'campaign_key': campaign_key,
        'dispatch_enabled': dispatch_enabled,
        'spine': _normalize_thread_payload(
            spine_raw, path='spine', role='spine',
        ),
        'fronts': fronts,
    }
    if isolated_world is not None:
        if {front['base_dungeon'] for front in fronts} != activity_dungeons:
            raise CampaignInputError('每个活动地图必须恰好对应一条地图前线')
        spec['isolated_world'] = isolated_world
    if payload.get('stage_plan') is not None:
        if not dispatch_enabled:
            raise CampaignInputError('阶段计划需要同时启用议事厅共同任务')
        from plugins.adventurer_s1.services.s1_campaign_stage_contract import normalize_stage_plan
        try:
            spec['stage_plan'] = normalize_stage_plan(payload['stage_plan'], {
                front['base_dungeon'] for front in fronts if front['role'] == 'dungeon_front'})
        except ValueError as exc:
            raise CampaignInputError(str(exc)) from exc
    return spec


def _exception_contains_integrity_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, IntegrityError):
            return True
        seen.add(id(current))
        nested = getattr(current, 'original_error', None)
        if not isinstance(nested, BaseException):
            nested = current.__cause__ or current.__context__
        current = nested if isinstance(nested, BaseException) else None
    return False


class S1WorldCampaignService:
    """Read, preview and atomically create operator-authored campaigns."""

    @staticmethod
    def _serialize_campaigns(
        roots: list[AdventurerS1WorldStoryThread],
    ) -> list[dict]:
        root_ids = [int(row.id) for row in roots]
        from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
        dispatch_by_root = S1CampaignDispatchRepository.admin_states(root_ids)
        fronts_by_root, links_by_thread, profiles_by_id = (
            S1WorldCampaignRepository.load_campaign_graph(root_ids)
        )
        result: list[dict] = []
        for root in roots:
            root_data = _serialize_thread(root)
            front_items = []
            for front in fronts_by_root.get(int(root.id), []):
                front_data = _serialize_thread(front)
                faction_items = []
                for link in links_by_thread.get(int(front.id), []):
                    profile = profiles_by_id.get(int(link.faction_profile_id))
                    faction_items.append({
                        'id': int(link.faction_profile_id),
                        'relation_role': link.relation_role or 'involved',
                        'faction_key': profile.faction_key if profile else '',
                        'canonical_name': profile.canonical_name if profile else '',
                        'status': profile.status if profile else '',
                        'revision': int(profile.revision or 1) if profile else None,
                        'missing': profile is None,
                    })
                front_data['factions'] = faction_items
                front_items.append(front_data)
            result.append({
                'campaign_key': root.campaign_key or '',
                'spec_hash': root.campaign_spec_hash or '',
                'isolated_world': root.campaign_world,
                'status': root.status or '',
                'revision': int(root.revision or 1),
                'updated_at': root_data.get('updated_at'),
                'spine': root_data,
                'dispatch': dispatch_by_root.get(root.id),
                'fronts': front_items,
                'front_count': len(front_items),
            })
        return result

    @staticmethod
    def get_meta(
        *,
        page: int = 1,
        per_page: int = 20,
        faction_status: str | None = 'active',
        keyword: str | None = None,
    ) -> dict:
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))
        status = str(faction_status or '').strip() or None
        if status and status not in FACTION_PROFILE_STATUSES:
            status = 'active'
        keyword_text = str(keyword or '').strip() or None
        profiles, total = S1WorldCampaignRepository.list_faction_profiles(
            page=page,
            per_page=per_page,
            status=status,
            keyword=keyword_text,
        )
        return {
            'dungeons': [
                str(item.get('name') or '').strip()
                for item in S1_ACTIVE_DUNGEONS
                if str(item.get('name') or '').strip()
            ],
            'activity_dungeon_tiers': [
                {'min_level': low, 'max_level': high}
                for low, high in sorted({(item['min_level'], item['max_level']) for item in S1_ACTIVE_DUNGEONS})
            ],
            'roles': list(CAMPAIGN_ROLES),
            'front_roles': list(CAMPAIGN_FRONT_ROLES),
            'world_story_statuses': list(WORLD_INTEL_STATUSES),
            'dungeon_front_thread_types': list(_DUNGEON_FRONT_THREAD_TYPES),
            'faction_profile_statuses': sorted(FACTION_PROFILE_STATUSES),
            'all_dungeons_scope': WORLD_INTEL_ALL_DUNGEONS_SCOPE,
            'tavern_scope_key': ABYSS_TAVERN_FACTION_SCOPE_KEY,
            'limits': {
                'max_fronts': _max_fronts(),
                'max_active_teams': _max_active_teams(),
                'max_profiles_per_front': _max_profiles_per_front(),
                'max_scope_keys_per_front': 8,
                'default_weight': _default_thread_weight(),
            },
            'presets': [{'key': 'baiwei_banquet', 'title': '百胃宴城·开宴之后（20队）'}],
            'faction_profiles': {
                'items': [_serialize_profile_option(row) for row in profiles],
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': (total + per_page - 1) // per_page if total else 0,
            },
        }

    @staticmethod
    def get_preset(key):
        """A source-controlled operator draft; reading it neither previews nor creates worlds."""
        if key != 'baiwei_banquet':
            return None
        from pathlib import Path
        import json
        source = Path(__file__).resolve().parents[1] / 'data' / 'campaigns' / 'baiwei_banquet.json'
        return _normalize_spec(json.loads(source.read_text(encoding='utf-8')))

    @staticmethod
    def list_campaigns(
        *,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        keyword: str | None = None,
    ) -> dict:
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))
        status_text = str(status or '').strip() or None
        if status_text and status_text not in WORLD_INTEL_STATUSES:
            status_text = None
        roots, total = S1WorldCampaignRepository.list_campaign_roots(
            page=page,
            per_page=per_page,
            status=status_text,
            keyword=str(keyword or '').strip() or None,
        )
        return {
            'items': S1WorldCampaignService._serialize_campaigns(roots),
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page if total else 0,
        }

    @staticmethod
    def preview_create(
        payload: object,
        *,
        admin_user_id: int,
    ) -> tuple[dict | None, str | None, int]:
        try:
            spec = _normalize_spec(payload)
        except CampaignInputError as exc:
            return None, str(exc), 400
        spec_hash = _canonical_hash(spec)
        campaign_key = spec['campaign_key']
        profile_ids = sorted({
            int(profile_id)
            for front in spec['fronts']
            for profile_id in front['faction_profile_ids']
        })
        profiles = S1WorldCampaignRepository.get_faction_profiles(profile_ids)
        profiles_by_id = {int(row.id): row for row in profiles}
        profile_revisions = [
            {'id': profile_id, 'revision': int(profiles_by_id[profile_id].revision or 1)}
            for profile_id in profile_ids
            if profile_id in profiles_by_id
        ]
        conflicts: list[dict[str, Any]] = []
        missing_ids = [value for value in profile_ids if value not in profiles_by_id]
        if missing_ids:
            conflicts.append({
                'code': 'missing_faction_profiles',
                'profile_ids': missing_ids,
            })
        inactive = [
            {
                'id': int(row.id),
                'canonical_name': row.canonical_name or '',
                'status': row.status or '',
            }
            for row in profiles
            if row.status != 'active'
        ]
        if inactive:
            conflicts.append({
                'code': 'inactive_faction_profiles',
                'profiles': inactive,
            })

        existing_root = S1WorldCampaignRepository.get_root_by_key(campaign_key)
        replayed = bool(
            existing_root
            and (existing_root.campaign_spec_hash or '') == spec_hash
        )
        if existing_root and not replayed:
            conflicts.append({
                'code': 'campaign_key_conflict',
                'campaign_key': campaign_key,
                'existing_root_id': int(existing_root.id),
                'existing_spec_hash': existing_root.campaign_spec_hash or '',
            })
        if not existing_root:
            if spec.get('isolated_world'):
                from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
                occupied = S1CampaignWorldRepository.occupied_names(
                    entry['name'] for entry in spec['isolated_world']['dungeons'])
                if occupied:
                    conflicts.append({'code': 'activity_dungeon_names_occupied', 'dungeons': occupied})
            titles = [spec['spine']['title'], *(
                front['title'] for front in spec['fronts']
            )]
            title_conflicts = S1WorldCampaignRepository.find_title_conflicts(titles)
            if title_conflicts:
                conflicts.append({
                    'code': 'world_story_title_conflicts',
                    'threads': [
                        {'id': int(row.id), 'title': row.title or ''}
                        for row in title_conflicts
                    ],
                })

        warnings: list[dict[str, Any]] = []
        dungeons = sorted({front['base_dungeon'] for front in spec['fronts']})
        input_titles = {
            spec['spine']['title'],
            *(front['title'] for front in spec['fronts']),
        }
        existing_threads = [
            row for row in S1WorldCampaignRepository.list_active_threads_for_dungeons(
                dungeons
            )
            if row.title not in input_titles
        ]
        threads_by_dungeon: dict[str, list[dict]] = {}
        for row in existing_threads:
            threads_by_dungeon.setdefault(row.base_dungeon or '', []).append({
                'id': int(row.id),
                'title': row.title or '',
                'campaign_root_id': row.campaign_root_id,
            })
        for dungeon, rows in threads_by_dungeon.items():
            warnings.append({
                'code': 'existing_mutable_threads',
                'base_dungeon': dungeon,
                'threads': rows,
            })
        profile_links = (
            S1WorldCampaignRepository.list_active_campaign_links_for_profiles(
                profile_ids
            )
        )
        if profile_links:
            warnings.append({
                'code': 'factions_already_in_active_campaign_fronts',
                'links': [
                    {
                        'faction_profile_id': int(link.faction_profile_id),
                        'thread_id': int(thread.id),
                        'campaign_root_id': int(thread.campaign_root_id),
                        'title': thread.title or '',
                    }
                    for link, thread in profile_links
                ],
            })

        can_execute = not conflicts or replayed
        snapshot = {
            'schema_version': CAMPAIGN_SCHEMA_VERSION,
            'normalized_spec': spec,
            'spec_hash': spec_hash,
            'profile_revisions': profile_revisions,
        }
        preview_token = None
        if can_execute:
            preview_token = S1AdminScanSnapshotService.store(
                CAMPAIGN_SNAPSHOT_OPERATION,
                int(admin_user_id),
                snapshot,
                allow_signed_fallback=True,
            )
            if not preview_token:
                return None, '无法创建可执行预览令牌，请稍后重试', 503
        existing_campaign = (
            S1WorldCampaignService._serialize_campaigns([existing_root])[0]
            if replayed and existing_root else None
        )
        return {
            'preview_token': preview_token,
            'spec_hash': spec_hash,
            'campaign': spec,
            'summary': {
                'spines': 1,
                'fronts': len(spec['fronts']),
                'dungeons': len(dungeons),
                'factions': len(profile_ids),
            },
            'profile_revisions': profile_revisions,
            'warnings': warnings,
            'conflicts': conflicts,
            'can_execute': can_execute,
            'replayed': replayed,
            'existing_campaign': existing_campaign,
        }, None, 200

    @staticmethod
    def create_from_preview(
        preview_token: object,
        *,
        admin_user_id: int,
    ) -> tuple[dict | None, str | None, int]:
        snapshot = S1AdminScanSnapshotService.load(
            CAMPAIGN_SNAPSHOT_OPERATION,
            int(admin_user_id),
            preview_token,
            allow_signed_fallback=True,
        )
        if not snapshot:
            return None, '预览令牌无效或已过期，请重新预览', 409
        if snapshot.get('schema_version') != CAMPAIGN_SCHEMA_VERSION:
            return None, '预览版本已过期，请重新预览', 409
        try:
            spec = _normalize_spec(snapshot.get('normalized_spec'))
        except CampaignInputError:
            return None, '预览内容无效，请重新预览', 409
        spec_hash = _canonical_hash(spec)
        if spec_hash != snapshot.get('spec_hash'):
            return None, '预览内容校验失败，请重新预览', 409
        frozen_profile_revisions = {
            int(item['id']): int(item['revision'])
            for item in (snapshot.get('profile_revisions') or [])
            if isinstance(item, dict)
            and isinstance(item.get('id'), int)
            and not isinstance(item.get('id'), bool)
            and isinstance(item.get('revision'), int)
            and not isinstance(item.get('revision'), bool)
        }
        expected_profile_ids = sorted({
            int(profile_id)
            for front in spec['fronts']
            for profile_id in front['faction_profile_ids']
        })

        try:
            with db_manager.transaction():
                S1WorldCampaignRepository.acquire_campaign_write_lock()
                existing_root = S1WorldCampaignRepository.get_root_by_key(
                    spec['campaign_key'], for_update=True,
                )
                if existing_root:
                    if (existing_root.campaign_spec_hash or '') != spec_hash:
                        return None, 'campaign_key 已对应不同启动合同', 409
                    campaign = S1WorldCampaignService._serialize_campaigns(
                        [existing_root]
                    )[0]
                    return {
                        'created': False,
                        'replayed': True,
                        'campaign': campaign,
                    }, None, 200

                if sorted(frozen_profile_revisions) != expected_profile_ids:
                    return None, '预览缺少完整势力版本，请重新预览', 409
                if spec.get('isolated_world'):
                    from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
                    occupied = S1CampaignWorldRepository.occupied_names(
                        entry['name'] for entry in spec['isolated_world']['dungeons'])
                    if occupied:
                        return None, '活动地图名已被占用，请重新预览：' + '、'.join(occupied), 409
                titles = [spec['spine']['title'], *(
                    front['title'] for front in spec['fronts']
                )]
                title_conflicts = S1WorldCampaignRepository.find_title_conflicts(
                    titles, for_update=True,
                )
                if title_conflicts:
                    return None, '世界剧情标题已被占用，请重新预览', 409

                profiles = S1WorldCampaignRepository.lock_faction_profiles(
                    expected_profile_ids
                )
                profiles_by_id = {int(row.id): row for row in profiles}
                if sorted(profiles_by_id) != expected_profile_ids:
                    return None, '势力档案已删除，请重新预览', 409
                stale_profiles = [
                    {
                        'id': profile_id,
                        'expected_revision': frozen_profile_revisions[profile_id],
                        'actual_revision': int(profiles_by_id[profile_id].revision or 1),
                        'status': profiles_by_id[profile_id].status or '',
                    }
                    for profile_id in expected_profile_ids
                    if (
                        int(profiles_by_id[profile_id].revision or 1)
                        != frozen_profile_revisions[profile_id]
                        or profiles_by_id[profile_id].status != 'active'
                    )
                ]
                if stale_profiles:
                    return None, f'势力档案已变化，请重新预览：{stale_profiles}', 409

                spine = spec['spine']
                root = AdventurerS1WorldStoryThread(
                    title=spine['title'],
                    thread_type='event',
                    status='seeded',
                    base_dungeon=WORLD_INTEL_ALL_DUNGEONS_SCOPE,
                    scope_keys=[WORLD_INTEL_ALL_DUNGEONS_SCOPE],
                    trigger_keywords=spine['trigger_keywords'],
                    summary=spine['summary'],
                    next_reveal_hint=spine['next_reveal_hint'] or None,
                    power_tier=spine['power_tier'] or None,
                    power_profile=spine['power_profile'] or None,
                    weight=spine['weight'],
                    revision=1,
                    is_chain_head=True,
                    campaign_key=spec['campaign_key'],
                    campaign_role='spine',
                    campaign_agenda=spine['campaign_agenda'],
                    campaign_spec_hash=spec_hash,
                    campaign_world=spec.get('isolated_world'),
                )
                S1WorldCampaignRepository.add_world_story_threads([root])
                S1WorldCampaignRepository.flush()
                root.campaign_root_id = int(root.id)
                root.chain_key = f'world-campaign:{int(root.id)}'

                fronts: list[AdventurerS1WorldStoryThread] = []
                for item in spec['fronts']:
                    fronts.append(AdventurerS1WorldStoryThread(
                        title=item['title'],
                        thread_type=item['thread_type'],
                        status='seeded',
                        base_dungeon=item['base_dungeon'],
                        scope_keys=item['scope_keys'],
                        trigger_keywords=item['trigger_keywords'],
                        summary=item['summary'],
                        next_reveal_hint=item['next_reveal_hint'] or None,
                        power_tier=item['power_tier'] or None,
                        power_profile=item['power_profile'] or None,
                        weight=item['weight'],
                        revision=1,
                        is_chain_head=True,
                        campaign_root_id=int(root.id),
                        campaign_role=item['role'],
                        campaign_agenda=item['campaign_agenda'],
                    ))
                S1WorldCampaignRepository.add_world_story_threads(fronts)
                S1WorldCampaignRepository.flush()
                for row in fronts:
                    row.chain_key = f'world-story:{int(row.id)}'

                links: list[AdventurerS1FactionProfileThread] = []
                for item, front in zip(spec['fronts'], fronts):
                    relation_role = (
                        'core' if item['role'] == 'faction_front' else 'involved'
                    )
                    for profile_id in item['faction_profile_ids']:
                        links.append(AdventurerS1FactionProfileThread(
                            faction_profile_id=int(profile_id),
                            world_story_thread_id=int(front.id),
                            relation_role=relation_role,
                        ))
                S1WorldCampaignRepository.add_faction_links(links)
                S1WorldCampaignRepository.flush()
                if spec.get('dispatch_enabled'):
                    from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
                    _dispatch, dispatch_error = S1CampaignDispatchRepository.enable(root.id)
                    if dispatch_error:
                        raise ValueError(dispatch_error)
                    if spec.get('isolated_world'):
                        from datetime import datetime
                        _dispatch.discovered_at = _portable_utc_now()
                        _dispatch.public_title = spine['title']
                        _dispatch.public_summary = spine['summary'][:1200]
                    if spec.get('stage_plan'):
                        from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository
                        S1CampaignStageRepository.create(_dispatch, spec['stage_plan'], revealed=bool(spec.get('isolated_world')))
                campaign = S1WorldCampaignService._serialize_campaigns([root])[0]
                return {
                    'created': True,
                    'replayed': False,
                    'campaign': campaign,
                }, None, 201
        except (DatabaseError, TransactionError) as exc:
            if _exception_contains_integrity_error(exc):
                return None, '共同战役写入冲突，请重新预览', 409
            current_app.logger.exception(
                'S1 共同战役创建失败 admin_user_id=%s campaign_key=%s',
                admin_user_id,
                spec.get('campaign_key'),
            )
            return None, '共同战役创建失败，请稍后重试', 500

    @staticmethod
    def patch_agenda(
        thread_id: int,
        payload: object,
        *,
        admin_user_id: int,
    ) -> tuple[dict | None, str | None, int]:
        if not isinstance(payload, dict):
            return None, '请求体必须是对象', 400
        try:
            agenda = _required_text(
                payload.get('campaign_agenda', payload.get('agenda')),
                'campaign_agenda',
                limit=4000,
            )
        except CampaignInputError as exc:
            return None, str(exc), 400
        source_revision = payload.get('source_revision')
        expected_revision = payload.get('expected_revision')
        if (
            source_revision is not None
            and expected_revision is not None
            and source_revision != expected_revision
        ):
            return None, 'source_revision 与 expected_revision 不一致', 400
        if source_revision is None:
            source_revision = expected_revision
        if (
            isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision <= 0
        ):
            return None, 'source_revision 或 expected_revision 必须是正整数', 400
        try:
            with db_manager.transaction():
                row = S1WorldCampaignRepository.get_campaign_thread_for_update(
                    int(thread_id)
                )
                if not row:
                    return None, '共同战役剧情节点不存在', 404
                current_revision = int(row.revision or 1)
                if current_revision != source_revision:
                    return None, (
                        f'剧情节点版本已变化：expected={source_revision}, '
                        f'actual={current_revision}'
                    ), 409
                if (row.campaign_agenda or '') == agenda:
                    return {
                        'updated': False,
                        'thread': _serialize_thread(row),
                    }, None, 200
                from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
                if S1CampaignDispatchRepository.root_is_occupied(row.campaign_root_id or row.id):
                    return None, '已有小队正在推进，请等待战报发布后再修改议程', 409
                row.campaign_agenda = agenda
                row.revision = current_revision + 1
                S1WorldCampaignRepository.flush()
                current_app.logger.info(
                    'S1 共同战役 agenda 已更新 admin_user_id=%s thread_id=%s revision=%s',
                    admin_user_id,
                    thread_id,
                    row.revision,
                )
                return {
                    'updated': True,
                    'thread': _serialize_thread(row),
                }, None, 200
        except (DatabaseError, TransactionError):
            current_app.logger.exception(
                'S1 共同战役 agenda 更新失败 admin_user_id=%s thread_id=%s',
                admin_user_id,
                thread_id,
            )
            return None, '共同战役 agenda 更新失败，请稍后重试', 500
