"""S1 探险日志前台可见内容构建（API 最小化，对齐 legacy adventurer_log_service）。

DB 内 `full_log` 保留 AI 结算全量；经 `get_log_detail` 下发给非管理员时，
由 `build_player_s1_full_log` 只保留前端渲染/交互所需字段，并将资产修改收敛为名称摘要。
"""
from __future__ import annotations

from plugins.adventurer.utils import sanitize_equipment_name
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1
from plugins.adventurer_s1.services.s1_visible_name_policy import (
    is_internal_code_identifier_name,
    resolve_legacy_visible_name,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)

# full_log 顶层：玩家详情弹窗白名单（events / summary / member_updates / status）
_S1_PUBLIC_FULL_LOG_TOP_KEYS = ('status', 'events', 'summary', 'member_updates')

# 顶层内部字段（推进状态、特殊事件掷骰、重复 target_dungeon、AI 收束校验等）
_S1_PUBLIC_LOG_INTERNAL_TOP_LEVEL_FIELDS = (
    'completion_check',
    'current_event_index',
    'progress_push_count',
    'has_special_event',
    'is_extreme_crisis',
    'special_event_type',
    'target_dungeon',
    'objective_outline',  # 剧情生成前客观局势草案 AI，仅管理员可见
    'pre_narrative_resolution',  # 冻结生命裁决证据，仅内部审核/申诉使用
    'pre_narrative_life_adjustments',  # 正文阶段生命变化链，仅内部审核/申诉使用
)

# member_updates 单条白名单：与 ExpeditionMemberUpdates.vue 展示字段对齐
_S1_PUBLIC_MEMBER_UPDATE_FIELDS = (
    'id',
    'name',
    'is_dead',
    'death_reason',
    'hp_change',
    'remaining_hp',
    'max_hp',
    'hp_before',
    'gold_change',
    'score',
    'new_class',
    'new_traits',
    'removed_traits',
    'new_skills',
    'removed_skills',
    'new_equipments',
    'discarded_equipments',
    'consumed_equipments',
    'strength_increase',
    'agility_increase',
    'intelligence_increase',
    'luck_increase',
)

_PUBLIC_EQUIPMENT_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})
_PUBLIC_ASSET_MUTATION_TYPES = frozenset({'trait', 'skill', 'equipment'})
_PUBLIC_ASSET_MUTATION_OPERATIONS = frozenset({'created', 'updated'})
_PUBLIC_ASSET_STATE_FIELDS = {
    'trait': ('name', 'desc', 'is_archived'),
    'skill': ('name', 'desc', 'equipped'),
    'equipment': (
        'name', 'type', 'rarity', 'desc', 'quantity', 'uses_left',
        'evolution_stage',
    ),
}
_PLAYER_MODIFIED_ASSET_FIELDS = {
    'trait': 'modified_traits',
    'skill': 'modified_skills',
    'equipment': 'modified_equipments',
}


def _minimize_new_equipments(raw: object) -> dict:
    """仅下发结算物品名称、稀有度、数量与玩家可读描述。"""
    if not isinstance(raw, dict):
        return {}
    minimized: dict = {}
    for name, data in raw.items():
        if not name:
            continue
        clean_name = resolve_legacy_visible_name(sanitize_equipment_name(str(name)))
        rarity = 'common'
        quantity = 1
        if isinstance(data, dict):
            raw_rarity = data.get('rarity')
            if raw_rarity in _PUBLIC_EQUIPMENT_RARITIES:
                rarity = raw_rarity
            normalized_quantity, _ = normalize_ascii_integer(
                data.get('quantity'),
                minimum=1,
                maximum=999999,
            )
            if type(normalized_quantity) is int:
                quantity = normalized_quantity
        minimized[clean_name] = {'rarity': rarity, 'quantity': quantity}
        if isinstance(data, dict):
            description = data.get('description')
            if not isinstance(description, str) or not description:
                description = data.get('desc')
            if isinstance(description, str) and description:
                minimized[clean_name]['desc'] = description
    return minimized


def _minimize_discarded_equipments(raw: object) -> list[dict]:
    """只公开后端确定的未到账字段，并重建可信提示文案。"""
    if not isinstance(raw, list):
        return []
    minimized: list[dict] = []
    for entry in raw[:100]:
        if not isinstance(entry, dict):
            continue
        reason = entry.get('reason')
        if reason not in ('warehouse_full', 'world_unique_conflict'):
            continue
        clean_name = resolve_legacy_visible_name(
            sanitize_equipment_name(str(entry.get('name') or '')),
        )
        if not clean_name:
            continue
        try:
            quantity = max(1, min(int(entry.get('quantity', 1)), 999999))
        except (TypeError, ValueError):
            quantity = 1
        if reason == 'world_unique_conflict':
            message = f'全服唯一物品「{clean_name}」当前已无法发放，本次未能到账'
        else:
            message = f'仓库已满，未能存入「{clean_name}」，该物品已丢弃'
        minimized.append({
            'name': clean_name,
            'quantity': quantity,
            'reason': reason,
            'message': message,
        })
    return minimized


def _sanitize_public_member_update_equipment_names(update: dict) -> dict:
    """清洗 member_update 里误拼进 key 的装备脏名（对齐 legacy 日志展示层）。"""
    sanitized = dict(update)
    raw_equip = sanitized.get('new_equipments')
    if isinstance(raw_equip, dict):
        sanitized['new_equipments'] = {
            resolve_legacy_visible_name(sanitize_equipment_name(str(name))): data
            for name, data in raw_equip.items()
            if name
        }
    return sanitized


def _sanitize_public_member_update_named_traits_and_skills(update: dict) -> dict:
    """历史日志只隐藏机械可证的内部标识名，不改原始 full_log。"""
    sanitized = dict(update)
    traits = sanitized.get('new_traits')
    if isinstance(traits, dict):
        sanitized['new_traits'] = {
            name: data
            for name, data in traits.items()
            if not is_internal_code_identifier_name(name)
        }
    elif isinstance(traits, list):
        sanitized['new_traits'] = [
            item for item in traits
            if not (
                isinstance(item, dict)
                and is_internal_code_identifier_name(item.get('name'))
            )
        ]

    skills = sanitized.get('new_skills')
    if isinstance(skills, list):
        sanitized['new_skills'] = [
            item for item in skills
            if not is_internal_code_identifier_name(
                item.get('name') if isinstance(item, dict) else item,
            )
        ]
    return sanitized


def _pick_public_asset_state(asset_type: str, raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    public = {
        key: raw[key]
        for key in _PUBLIC_ASSET_STATE_FIELDS[asset_type]
        if key in raw
    }
    name = str(public.get('name') or '').strip()
    if not name:
        return None
    if asset_type == 'equipment':
        public['name'] = resolve_legacy_visible_name(sanitize_equipment_name(name))
    elif is_internal_code_identifier_name(name):
        return None
    else:
        public['name'] = name
    return public


def _collect_public_asset_changes(full_log: dict) -> dict[int, list[dict]]:
    """从通过的后端到账回执投影玩家安全流水，不信任 AI member_update。"""
    receipt = full_log.get('settlement_receipt')
    if not isinstance(receipt, dict) or receipt.get('status') != 'passed':
        return {}

    changes_by_member: dict[int, list[dict]] = {}
    for member in receipt.get('members') or []:
        if not isinstance(member, dict) or member.get('status') != 'passed':
            continue
        try:
            member_id = int(member.get('adventurer_id'))
        except (TypeError, ValueError):
            continue
        changes: list[dict] = []
        for mutation in (member.get('asset_mutations') or [])[:30]:
            if not isinstance(mutation, dict):
                continue
            asset_type = str(mutation.get('asset_type') or '').strip()
            operation = str(mutation.get('operation') or '').strip()
            if (
                asset_type not in _PUBLIC_ASSET_MUTATION_TYPES
                or operation not in _PUBLIC_ASSET_MUTATION_OPERATIONS
            ):
                continue
            before = _pick_public_asset_state(asset_type, mutation.get('before'))
            after = _pick_public_asset_state(asset_type, mutation.get('after'))
            if after is None:
                continue
            changes.append({
                'asset_type': asset_type,
                'operation': operation,
                'before': before,
                'after': after,
            })
        if changes:
            changes_by_member[member_id] = changes
    return changes_by_member


def _summarize_player_asset_modifications(changes: object) -> dict[str, list[str]]:
    """玩家结算只展示修改后的名称；完整 before/after 继续留给内部审核链。"""
    summaries: dict[str, list[str]] = {}
    seen_by_field: dict[str, set[str]] = {}
    if not isinstance(changes, list):
        return summaries
    for change in changes:
        if not isinstance(change, dict) or change.get('operation') != 'updated':
            continue
        field = _PLAYER_MODIFIED_ASSET_FIELDS.get(change.get('asset_type'))
        after = change.get('after')
        if not field or not isinstance(after, dict):
            continue
        name = str(after.get('name') or '').strip()
        if not name or name in seen_by_field.setdefault(field, set()):
            continue
        seen_by_field[field].add(name)
        summaries.setdefault(field, []).append(name)
    return summaries


def _collect_settled_new_trait_names(full_log: object) -> dict[int, list[str]]:
    """有完整到账回执时，新增提示只采用本次实际创建的特质。"""
    if not isinstance(full_log, dict):
        return {}
    receipt = full_log.get('settlement_receipt')
    if (
        not isinstance(receipt, dict)
        or receipt.get('schema_version') != 2
        or receipt.get('status') != 'passed'
    ):
        return {}
    names_by_member: dict[int, list[str]] = {}
    for member in receipt.get('members') or []:
        if not isinstance(member, dict) or member.get('status') != 'passed':
            continue
        mutations = member.get('asset_mutations')
        if 'traits' not in (member.get('checked') or []) or not isinstance(mutations, list):
            continue
        member_id, _ = normalize_ascii_integer(member.get('adventurer_id'), minimum=1)
        if type(member_id) is not int:
            continue
        names: list[str] = []
        # 必须读取完整回执；展示流水的数量上限不能截掉实际到账的特质。
        for mutation in mutations:
            if (
                not isinstance(mutation, dict)
                or mutation.get('asset_type') != 'trait'
                or mutation.get('operation') != 'created'
            ):
                continue
            after = _pick_public_asset_state('trait', mutation.get('after'))
            if after and after['name'] not in names:
                names.append(after['name'])
        # 空数组也是已核验结果：已有特质原样重复输出不算新增。
        names_by_member[member_id] = names
    return names_by_member


def _pick_public_member_update(update: object, name_by_id: dict[int, str]) -> dict | None:
    if not isinstance(update, dict):
        return None
    adv_id = update.get('id')
    try:
        adv_id_int = int(adv_id) if adv_id is not None else None
    except (TypeError, ValueError):
        adv_id_int = None

    public: dict = {}
    for key in _S1_PUBLIC_MEMBER_UPDATE_FIELDS:
        if key not in update:
            continue
        if key == 'death_reason' and update.get('is_dead') is not True:
            continue
        if key == 'new_equipments':
            minimized = _minimize_new_equipments(update.get('new_equipments'))
            if minimized:
                public['new_equipments'] = minimized
            continue
        if key == 'discarded_equipments':
            minimized = _minimize_discarded_equipments(update.get('discarded_equipments'))
            if minimized:
                public['discarded_equipments'] = minimized
            continue
        public[key] = update[key]

    if adv_id_int is not None:
        public['id'] = adv_id_int
        if not public.get('name') and adv_id_int in name_by_id:
            public['name'] = name_by_id[adv_id_int]

    return public or None


def _collect_settled_inventory_names(full_log: object) -> dict[int, set[str]]:
    """完整冻结清单只用于消耗提示去伪，不向玩家下发出发内部数据。"""
    if not isinstance(full_log, dict):
        return {}
    receipt = full_log.get('settlement_receipt')
    if not isinstance(receipt, dict) or receipt.get('status') != 'passed':
        return {}
    inventory_by_member: dict[int, set[str]] = {}
    for snapshot in full_log.get('audit_member_snapshot') or []:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('equipment'), list):
            continue
        member_id, _ = normalize_ascii_integer(snapshot.get('adventurer_id'), minimum=1)
        if type(member_id) is not int:
            continue
        inventory_by_member[member_id] = {
            str(item.get('name') or '').strip()
            for item in snapshot['equipment'] if isinstance(item, dict)
        }
    for update in full_log.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        member_id, _ = normalize_ascii_integer(update.get('id'), minimum=1)
        if member_id not in inventory_by_member:
            continue
        new_equipment = update.get('new_equipments') or {}
        if not isinstance(new_equipment, dict):
            # 老格式无法建立完整清单时保持历史展示。
            inventory_by_member.pop(member_id, None)
            continue
        inventory_by_member[member_id].update(str(name).strip() for name in new_equipment)
    return inventory_by_member


def _collect_member_ids(full_log: dict) -> list[int]:
    ids: list[int] = []
    for update in full_log.get('member_updates') or []:
        if not isinstance(update, dict) or update.get('id') is None:
            continue
        try:
            ids.append(int(update['id']))
        except (TypeError, ValueError):
            continue
    return ids


def _fetch_adventurer_names(member_ids: list[int]) -> dict[int, str]:
    if not member_ids:
        return {}
    rows = (
        AdventurerS1.query
        .with_entities(AdventurerS1.id, AdventurerS1.name)
        .filter(AdventurerS1.id.in_(member_ids))
        .all()
    )
    return {row[0]: row[1] for row in rows}


def _resolve_member_names_from_full_log(full_log: dict) -> list[str]:
    """从 full_log 的 members / member_updates 提取名字（不含 party 回退）。"""
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: object) -> None:
        if not name:
            return
        text = str(name).strip()
        if text and text not in seen:
            seen.add(text)
            names.append(text)

    raw_members = full_log.get('members')
    if isinstance(raw_members, list):
        for item in raw_members:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                _add(item.get('name'))

    if names:
        return names

    for update in full_log.get('member_updates') or []:
        if isinstance(update, dict):
            _add(update.get('name'))

    if names:
        return names

    member_ids = _collect_member_ids(full_log)
    if not member_ids:
        return []
    name_by_id = _fetch_adventurer_names(member_ids)
    for mid in member_ids:
        _add(name_by_id.get(mid))
    return names


def resolve_expedition_log_member_names(log, *full_logs: dict | None) -> list[str]:
    """日志详情顶层 members：仅名字字符串，供 UI 名字高亮；不写入 full_log。

    优先从小队关联解析（generating 阶段 full_log 常无 members）；
    再回退候选 full_log.members / member_updates。
    """
    if log.party_id:
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1PartyMember
        rows = (
            db.session.query(AdventurerS1.name)
            .join(
                AdventurerS1PartyMember,
                AdventurerS1PartyMember.adventurer_id == AdventurerS1.id,
            )
            .filter(AdventurerS1PartyMember.party_id == log.party_id)
            .order_by(AdventurerS1PartyMember.id.asc())
            .all()
        )
        party_names = [name for (name,) in rows if name]
        if party_names:
            return party_names

    for full_log in full_logs:
        if not isinstance(full_log, dict) or not full_log:
            continue
        names = _resolve_member_names_from_full_log(full_log)
        if names:
            return names
    return []


def build_public_s1_full_log(full_log: dict | None) -> dict:
    """把 completed/failed 日志的 full_log 裁成玩家可见白名单。"""
    if not isinstance(full_log, dict):
        return {
            'status': 'completed',
            'events': [],
            'summary': '',
            'member_updates': [],
        }

    name_by_id = _fetch_adventurer_names(_collect_member_ids(full_log))
    asset_changes_by_member = _collect_public_asset_changes(full_log)

    public_updates: list[dict] = []
    for update in full_log.get('member_updates') or []:
        if isinstance(update, dict):
            update = _sanitize_public_member_update_equipment_names(update)
            update = _sanitize_public_member_update_named_traits_and_skills(update)
        picked = _pick_public_member_update(update, name_by_id)
        if picked:
            member_id = picked.get('id')
            if member_id in asset_changes_by_member:
                picked['asset_changes'] = asset_changes_by_member[member_id]
            transfers = []
            for receipt in (full_log.get('settlement_receipt') or {}).get('asset_transfers') or []:
                if receipt.get('asset_kind') not in ('skill', 'trait'):
                    continue
                if member_id not in (receipt.get('from_adventurer_id'), receipt.get('to_adventurer_id')):
                    continue
                transfers.append({
                    'name': resolve_legacy_visible_name(str(receipt.get('name') or '')),
                    'kind': receipt['asset_kind'],
                    'direction': 'received' if member_id == receipt['to_adventurer_id'] else 'sent',
                })
            if transfers:
                picked['asset_transfers'] = transfers
            public_updates.append(picked)

    public: dict = {
        'status': full_log.get('status', 'completed'),
        'events': list(full_log.get('events') or []),
        'summary': full_log.get('summary', ''),
        'member_updates': public_updates,
    }
    # 只保留白名单顶层键（防御性：忽略任何未列出的键）
    return {key: public[key] for key in _S1_PUBLIC_FULL_LOG_TOP_KEYS if key in public}


def build_player_s1_full_log(full_log: dict | None) -> dict:
    """玩家日志投影：资产变更只返回“修改”名称，不下发详细前后状态。"""
    public = build_public_s1_full_log(full_log)
    settled_trait_names = _collect_settled_new_trait_names(full_log)
    settled_inventory_names = _collect_settled_inventory_names(full_log)
    player_updates: list[dict] = []
    for update in public.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        player_update = dict(update)
        changes = player_update.pop('asset_changes', None)
        player_update.update(_summarize_player_asset_modifications(changes))
        member_id = player_update.get('id')
        if 'new_traits' in player_update and member_id in settled_trait_names:
            player_update['new_traits'] = settled_trait_names[member_id]
        consumed = player_update.get('consumed_equipments')
        if isinstance(consumed, list) and member_id in settled_inventory_names:
            player_update['consumed_equipments'] = [
                entry for entry in consumed
                if str(entry.get('name') if isinstance(entry, dict) else entry).strip()
                in settled_inventory_names[member_id]
            ]
        player_updates.append(player_update)
    public['member_updates'] = player_updates
    return public


def build_in_flight_public_full_log(full_log: dict, *, log_status: str) -> dict:
    """generating / in_progress 玩家侧可见片段（无剧透、无内部字段）。"""
    events = full_log.get('events', []) or []
    if log_status == 'in_progress':
        idx = max(0, int(full_log.get('current_event_index', 0) or 0))
        visible_events = events[:idx]
        summary = '探险正在进行中...'
    else:
        # generating 阶段的事件前缀尚未被主探险结果确认；旧日志即使把前缀放在
        # events 中也必须隐藏，避免生成失败时提前剧透用餐、特殊事件或隐藏 Boss。
        visible_events = []
        departure_msg = full_log.get('departure_msg') if isinstance(full_log, dict) else None
        summary = departure_msg or '队伍正在赶往目标地牢……'

    public = {
        'status': log_status,
        'events': visible_events,
        'summary': summary,
        'member_updates': [],
    }
    if log_status == 'generating' and isinstance(full_log, dict) and full_log.get('departure_msg'):
        public['departure_msg'] = full_log['departure_msg']
    return public
