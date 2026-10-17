"""S1 探险中既有技能/装备的稳定 ID 修改合同。

这里只做结构化、身份和边界校验，不从剧情文本猜测是否应该修改。主剧情与
Observer 负责语义判断；最终消费者仍须按冻结快照锁行重检后再写回。
"""

from __future__ import annotations

from utils.ai_field_names import normalize_ai_description_field

from plugins.adventurer_s1.s1_item_types import (
    S1_INTERNAL_ITEM_TYPE_SET,
    S1_ITEM_TYPE_SET,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


S1_SKILL_UPDATES_FIELD = 'skill_updates'
S1_EQUIPMENT_UPDATES_FIELD = 'equipment_updates'
S1_EXISTING_ASSET_UPDATE_ERROR_MARKER = '既有技能/装备专用修改协议错误'

_VALID_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})
_VALID_ITEM_TYPES = S1_ITEM_TYPE_SET | S1_INTERNAL_ITEM_TYPE_SET
_SKILL_UPDATE_KEYS = frozenset({
    'skill_id', 'name', 'desc', 'recovery_minutes', 'evidence_event_indexes',
})
_EQUIPMENT_UPDATE_KEYS = frozenset({
    'equipment_id', 'name', 'type', 'rarity', 'desc', 'uses_left',
    'evolution_stage', 'recovery_minutes', 'evidence_event_indexes',
})
_BACKEND_RECOVERABLE_SKILL_UPDATE_KEYS = frozenset({'recovery_minutes'})
_BACKEND_RECOVERABLE_EQUIPMENT_UPDATE_KEYS = frozenset({
    'type', 'rarity', 'uses_left', 'evolution_stage', 'recovery_minutes',
})
_MAX_DESCRIPTION_LENGTH = 4000


def _is_lossless_complete_revision(
    authoritative_description: str,
    proposed_description: str,
) -> bool:
    """识别只插入新文字、未删除或重排底稿字符的完整当前描述。"""
    if (
        not authoritative_description
        or not proposed_description
        or len(proposed_description) < len(authoritative_description)
    ):
        return False
    proposed_index = 0
    for authoritative_char in authoritative_description:
        proposed_index = proposed_description.find(
            authoritative_char,
            proposed_index,
        )
        if proposed_index < 0:
            return False
        proposed_index += 1
    return True


def _is_complete_equipment_revision(
    authoritative_description: str,
    proposed_description: str,
) -> bool:
    """识别无损插入或逐字保留绝大部分底稿的装备完整修订。"""
    if not authoritative_description or not proposed_description:
        return False
    if _is_lossless_complete_revision(
        authoritative_description,
        proposed_description,
    ):
        return True
    common_prefix_length = 0
    for authoritative_char, proposed_char in zip(
        authoritative_description,
        proposed_description,
    ):
        if authoritative_char != proposed_char:
            break
        common_prefix_length += 1
    return (
        common_prefix_length > 0
        and common_prefix_length * 10 >= len(authoritative_description) * 9
    )


def preserve_s1_equipment_public_description(
    authoritative_description: object,
    proposed_description: object,
) -> str:
    """保留玩家可见全文，并按原有自由文案自然合并剧情更新。"""
    authoritative = str(authoritative_description or '').strip()
    proposed = str(proposed_description or '').strip()
    if not authoritative:
        return proposed
    if not proposed or proposed == authoritative or proposed in authoritative:
        return authoritative
    merged_prefix = f'{authoritative}\n\n'
    if proposed.startswith(merged_prefix):
        appended = proposed[len(merged_prefix):].strip()
        if _is_complete_equipment_revision(authoritative, appended):
            return appended
    if authoritative in proposed:
        return proposed
    if _is_complete_equipment_revision(authoritative, proposed):
        return proposed
    return f'{authoritative}\n\n{proposed}'


def preserve_s1_authoritative_public_description(
    authoritative_description: object,
    proposed_description: object,
) -> str:
    """严格保留冻结全文；无损完整修订去重，其余候选作为更新段追加。"""
    authoritative = str(authoritative_description or '').strip()
    proposed = str(proposed_description or '').strip()
    if not authoritative:
        return proposed
    if not proposed or proposed == authoritative or proposed in authoritative:
        return authoritative
    merged_prefix = f'{authoritative}\n\n'
    if proposed.startswith(merged_prefix):
        appended = proposed[len(merged_prefix):].strip()
        if _is_lossless_complete_revision(authoritative, appended):
            return appended
        return proposed
    if authoritative in proposed or _is_lossless_complete_revision(
        authoritative,
        proposed,
    ):
        return proposed
    return f'{authoritative}\n\n{proposed}'


def _non_empty_text(
    value: object,
    *,
    path: str,
    max_length: int,
) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f'{path}: 必须是非空字符串'
    normalized = value.strip()
    if len(normalized) > max_length:
        return None, f'{path}: 长度不能超过 {max_length}'
    return normalized, None


def _event_indexes(
    value: object,
    *,
    path: str,
    event_count: int | None,
) -> tuple[list[int] | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, f'{path}: 必须是非空数组'
    normalized: list[int] = []
    seen: set[int] = set()
    for index, raw_index in enumerate(value):
        event_index, _ = normalize_ascii_integer(raw_index, minimum=0)
        if type(event_index) is not int or event_index < 0:
            return None, f'{path}.{index}: 必须是非负整数'
        if event_count is not None and event_index >= event_count:
            return None, f'{path}.{index}: 超出 events 范围'
        if event_index in seen:
            return None, f'{path}.{index}: 不得重复引用同一事件'
        seen.add(event_index)
        normalized.append(event_index)
    return normalized, None


def _asset_rows(
    member_asset_context: dict | None,
    field_name: str,
) -> dict[int, dict]:
    if not isinstance(member_asset_context, dict):
        return {}
    value = member_asset_context.get(field_name)
    if not isinstance(value, dict):
        return {}
    rows: dict[int, dict] = {}
    for raw_id, raw_row in value.items():
        asset_id, _ = normalize_ascii_integer(raw_id, minimum=1)
        if type(asset_id) is int and asset_id > 0 and isinstance(raw_row, dict):
            rows[asset_id] = raw_row
    return rows


def _audit_snapshot_asset_rows(
    value: object,
    *,
    asset_kind: str,
) -> dict[int, dict]:
    """把冻结审核快照投影成既有资产规范化器使用的 ID 索引。"""
    if not isinstance(value, list):
        return {}
    rows: dict[int, dict] = {}
    ambiguous_ids: set[int] = set()
    for raw_row in value:
        if not isinstance(raw_row, dict):
            continue
        asset_id, _ = normalize_ascii_integer(
            raw_row.get('asset_id'), minimum=1,
        )
        if type(asset_id) is not int or asset_id <= 0:
            continue
        if asset_id in rows or asset_id in ambiguous_ids:
            rows.pop(asset_id, None)
            ambiguous_ids.add(asset_id)
            continue
        if asset_kind == 'equipment':
            rows[asset_id] = {
                'name': raw_row.get('name'),
                'type': raw_row.get('type'),
                'rarity': raw_row.get('rarity'),
                'immutable': raw_row.get('immutable') is True,
                'desc': raw_row.get('custom_desc') or raw_row.get('description'),
                'uses_left': raw_row.get('uses_left'),
                # 普通装备的冻结快照为节省体积会省略零阶；零阶是其唯一
                # 无损规范值。显式非法值仍保留给后续严格校验报错。
                'evolution_stage': raw_row.get('evolution_stage', 0),
                'recovery_minutes': raw_row.get('recovery_minutes', 0),
            }
        else:
            rows[asset_id] = {
                'name': raw_row.get('name'),
                'desc': raw_row.get('description'),
                'recovery_minutes': raw_row.get('recovery_minutes', 0),
            }
    return rows


def build_s1_existing_asset_context_by_member(
    audit_member_snapshot: object,
) -> dict[int, dict]:
    """从冻结审核快照构造既有资产后端补齐上下文。

    重复成员或重复资产 ID 无法唯一授权写回，因此从可补齐上下文中剔除；
    后续 validator 收到这份权威上下文后，会拒绝对应的任何既有资产修改。
    """
    if not isinstance(audit_member_snapshot, list):
        return {}
    contexts: dict[int, dict] = {}
    ambiguous_member_ids: set[int] = set()
    for raw_member in audit_member_snapshot:
        if not isinstance(raw_member, dict):
            continue
        member_id, _ = normalize_ascii_integer(
            raw_member.get('adventurer_id'), minimum=1,
        )
        if type(member_id) is not int or member_id <= 0:
            continue
        if member_id in contexts or member_id in ambiguous_member_ids:
            contexts.pop(member_id, None)
            ambiguous_member_ids.add(member_id)
            continue
        contexts[member_id] = {
            'equipment_assets_by_id': _audit_snapshot_asset_rows(
                raw_member.get('equipment'),
                asset_kind='equipment',
            ),
            'skill_assets_by_id': _audit_snapshot_asset_rows(
                raw_member.get('skills'),
                asset_kind='skill',
            ),
        }
    return contexts


def _normalize_skill_updates(
    value: object,
    *,
    path: str,
    assets: dict[int, dict],
    require_asset_context: bool,
    event_count: int | None,
) -> tuple[list[dict] | None, list[str]]:
    if not isinstance(value, list):
        return None, [f'{path}: 必须是数组']
    if require_asset_context and len(value) > len(assets):
        return None, [f'{path}: 条目数超过该成员可修改的出发技能数']

    errors: list[str] = []
    normalized: list[dict] = []
    seen_ids: set[int] = set()
    seen_final_names: set[str] = set()
    for index, raw_entry in enumerate(value):
        entry_path = f'{path}.{index}'
        if not isinstance(raw_entry, dict):
            errors.append(f'{entry_path}: 必须是对象')
            continue
        entry = {
            key: item for key, item in normalize_ai_description_field(raw_entry).items()
            if key in _SKILL_UPDATE_KEYS
        }
        required_keys = (
            _SKILL_UPDATE_KEYS - _BACKEND_RECOVERABLE_SKILL_UPDATE_KEYS
            if require_asset_context
            else _SKILL_UPDATE_KEYS
        )
        missing_keys = required_keys - frozenset(entry)
        if missing_keys:
            errors.append(f'{entry_path}: 缺少必填字段 {sorted(missing_keys)}')
            continue
        skill_id, _ = normalize_ascii_integer(entry.get('skill_id'), minimum=1)
        if type(skill_id) is not int or skill_id <= 0:
            errors.append(f'{entry_path}.skill_id: 必须是正整数')
            continue
        if skill_id in seen_ids:
            errors.append(f'{entry_path}.skill_id: 同一技能 ID 不得重复')
            continue
        seen_ids.add(skill_id)
        if require_asset_context and skill_id not in assets:
            errors.append(f'{entry_path}.skill_id: 不属于该成员本次出发技能快照')
            continue
        name, name_error = _non_empty_text(
            entry.get('name'), path=f'{entry_path}.name', max_length=100,
        )
        desc, desc_error = _non_empty_text(
            entry.get('desc'),
            path=f'{entry_path}.desc',
            max_length=_MAX_DESCRIPTION_LENGTH,
        )
        evidence, evidence_error = _event_indexes(
            entry.get('evidence_event_indexes'),
            path=f'{entry_path}.evidence_event_indexes',
            event_count=event_count,
        )
        source = assets.get(skill_id) or {}
        raw_recovery_minutes = (
            entry.get('recovery_minutes')
            if 'recovery_minutes' in entry
            else source.get('recovery_minutes', 0)
        )
        recovery_minutes, _ = normalize_ascii_integer(
            raw_recovery_minutes, minimum=0,
        )
        if type(recovery_minutes) is not int or recovery_minutes < 0:
            errors.append(f'{entry_path}.recovery_minutes: 必须是非负整数分钟')
            recovery_minutes = None
        unchanged = bool(
            require_asset_context
            and name is not None and desc is not None
            and recovery_minutes is not None
            and name == str(source.get('name') or '').strip()
            and desc == str(source.get('desc') or source.get('description') or '').strip()
            and recovery_minutes == int(source.get('recovery_minutes') or 0)
        )
        if unchanged and entry.get('evidence_event_indexes') == []:
            # 精确冻结身份与全部可写字段证明没有变化；空证据不产生任何
            # 写入或审核权限。真实变更和非法非空证据仍走原严格合同。
            continue
        for error in (name_error, desc_error, evidence_error):
            if error:
                errors.append(error)
        if name is None or desc is None or recovery_minutes is None or evidence is None:
            continue
        if name in seen_final_names:
            errors.append(f'{entry_path}.name: 同一成员的技能修改后名称不得重复')
            continue
        seen_final_names.add(name)

        if require_asset_context:
            if unchanged:
                # 完整字段已经机械证明返程状态等于冻结出发状态。该条没有
                # 任何写回动作，投影为空比把无操作升级成审核失败更准确。
                continue
            for other_id, other in assets.items():
                if other_id != skill_id and name == str(other.get('name') or '').strip():
                    errors.append(
                        f'{entry_path}.name: 与该成员另一条技能 ID {other_id} 的名称冲突'
                    )
                    break
            else:
                normalized.append({
                    'skill_id': skill_id,
                    'name': name,
                    'desc': desc,
                    'recovery_minutes': recovery_minutes,
                    'evidence_event_indexes': evidence,
                })
                continue
            continue
        normalized.append({
            'skill_id': skill_id,
            'name': name,
            'desc': desc,
            'recovery_minutes': recovery_minutes,
            'evidence_event_indexes': evidence,
        })
    return normalized, errors


def _normalize_equipment_updates(
    value: object,
    *,
    path: str,
    assets: dict[int, dict],
    require_asset_context: bool,
    event_count: int | None,
) -> tuple[list[dict] | None, list[str]]:
    if not isinstance(value, list):
        return None, [f'{path}: 必须是数组']
    if require_asset_context and len(value) > len(assets):
        return None, [f'{path}: 条目数超过该成员可修改的出发装备数']

    errors: list[str] = []
    normalized: list[dict] = []
    seen_ids: set[int] = set()
    seen_final_names: set[str] = set()
    for index, raw_entry in enumerate(value):
        entry_path = f'{path}.{index}'
        if not isinstance(raw_entry, dict):
            errors.append(f'{entry_path}: 必须是对象')
            continue
        entry = {
            key: item for key, item in normalize_ai_description_field(raw_entry).items()
            if key in _EQUIPMENT_UPDATE_KEYS
        }
        required_keys = (
            _EQUIPMENT_UPDATE_KEYS - _BACKEND_RECOVERABLE_EQUIPMENT_UPDATE_KEYS
            if require_asset_context
            else _EQUIPMENT_UPDATE_KEYS
        )
        missing_keys = required_keys - frozenset(entry)
        if missing_keys:
            errors.append(f'{entry_path}: 缺少必填字段 {sorted(missing_keys)}')
            continue
        equipment_id, _ = normalize_ascii_integer(
            entry.get('equipment_id'), minimum=1,
        )
        if type(equipment_id) is not int or equipment_id <= 0:
            errors.append(f'{entry_path}.equipment_id: 必须是正整数')
            continue
        if equipment_id in seen_ids:
            errors.append(f'{entry_path}.equipment_id: 同一装备 ID 不得重复')
            continue
        seen_ids.add(equipment_id)
        if require_asset_context and equipment_id not in assets:
            errors.append(f'{entry_path}.equipment_id: 不属于该成员本次出发装备快照')
            continue
        if require_asset_context and assets[equipment_id].get('immutable') is True:
            # 无字段修改权限；不让未生效的提案进入描述补全或最终写回。
            continue

        name, name_error = _non_empty_text(
            entry.get('name'), path=f'{entry_path}.name', max_length=100,
        )
        desc, desc_error = _non_empty_text(
            entry.get('desc'),
            path=f'{entry_path}.desc',
            max_length=_MAX_DESCRIPTION_LENGTH,
        )
        source = assets.get(equipment_id) or {}
        raw_type = entry.get('type')
        proposed_type = raw_type.strip() if isinstance(raw_type, str) else ''
        source_type = str(source.get('type') or '').strip()
        item_type = source_type if require_asset_context else proposed_type
        if item_type not in _VALID_ITEM_TYPES:
            errors.append(f'{entry_path}.type: 不是系统物品类型白名单值')
        raw_rarity = entry['rarity'] if 'rarity' in entry else source.get('rarity')
        rarity = raw_rarity.strip() if isinstance(raw_rarity, str) else ''
        if rarity not in _VALID_RARITIES:
            errors.append(f'{entry_path}.rarity: 不是合法稀有度')
        raw_uses_left = (
            entry.get('uses_left')
            if 'uses_left' in entry
            else source.get('uses_left')
        )
        uses_left: int | None
        uses_left_valid = True
        if raw_uses_left is None:
            uses_left = None
        else:
            uses_left, _ = normalize_ascii_integer(raw_uses_left, minimum=0)
            if type(uses_left) is not int or uses_left < 0:
                errors.append(f'{entry_path}.uses_left: 必须是 null 或非负整数')
                uses_left = None
                uses_left_valid = False
        source_stage, _ = normalize_ascii_integer(
            source.get('evolution_stage', 0), minimum=0,
        )
        source_stage_valid = (
            type(source_stage) is int and source_stage >= 0
        )
        if require_asset_context:
            # 剧情写回没有装备类型或伙伴阶级的修改权限。AI 可以省略，
            # 即使误填也无权覆盖同一稳定 ID 的冻结值。
            if not source_stage_valid:
                errors.append(
                    f'{entry_path}.evolution_stage: 冻结快照不是非负整数'
                )
                evolution_stage = None
            else:
                evolution_stage = source_stage
        else:
            evolution_stage, _ = normalize_ascii_integer(
                entry.get('evolution_stage'), minimum=0,
            )
            if type(evolution_stage) is not int or evolution_stage < 0:
                errors.append(f'{entry_path}.evolution_stage: 必须是非负整数')
                evolution_stage = None
        raw_recovery_minutes = (
            entry.get('recovery_minutes')
            if 'recovery_minutes' in entry
            else source.get('recovery_minutes', 0)
        )
        recovery_minutes, _ = normalize_ascii_integer(
            raw_recovery_minutes, minimum=0,
        )
        if type(recovery_minutes) is not int or recovery_minutes < 0:
            errors.append(f'{entry_path}.recovery_minutes: 必须是非负整数分钟')
            recovery_minutes = None
        evidence, evidence_error = _event_indexes(
            entry.get('evidence_event_indexes'),
            path=f'{entry_path}.evidence_event_indexes',
            event_count=event_count,
        )
        for error in (name_error, desc_error, evidence_error):
            if error:
                errors.append(error)
        if (
            name is None
            or desc is None
            or item_type not in _VALID_ITEM_TYPES
            or rarity not in _VALID_RARITIES
            or not uses_left_valid
            or evolution_stage is None
            or recovery_minutes is None
            or evidence is None
        ):
            continue
        if name in seen_final_names:
            errors.append(f'{entry_path}.name: 同一成员的装备修改后名称不得重复')
            continue
        seen_final_names.add(name)

        if require_asset_context:
            source_uses_left = source.get('uses_left')
            source_description = str(
                source.get('desc') or source.get('description') or ''
            ).strip()
            desc = preserve_s1_equipment_public_description(
                source_description,
                desc,
            )
            if (
                name == str(source.get('name') or '').strip()
                and item_type == str(source.get('type') or '').strip()
                and rarity == str(source.get('rarity') or '').strip()
                and desc == source_description
                and uses_left == source_uses_left
                and evolution_stage == source_stage
                and recovery_minutes == int(source.get('recovery_minutes') or 0)
            ):
                # 包括不可变字段归一后形成的无操作：不授予写权限，也不
                # 为已经可机械确定的相同状态增加 Observer 或主剧情重试。
                continue
            conflicting_id = next((
                other_id
                for other_id, other in assets.items()
                if other_id != equipment_id
                and name == str(other.get('name') or '').strip()
            ), None)
            if conflicting_id is not None:
                errors.append(
                    f'{entry_path}.name: 与该成员另一件装备 ID '
                    f'{conflicting_id} 的冻结名称冲突'
                )
                continue
        normalized.append({
            'equipment_id': equipment_id,
            'name': name,
            'type': item_type,
            'rarity': rarity,
            'desc': desc,
            'uses_left': uses_left,
            'evolution_stage': evolution_stage,
            'recovery_minutes': recovery_minutes,
            'evidence_event_indexes': evidence,
        })
    return normalized, errors


def normalize_s1_existing_asset_updates(
    member_update: dict,
    *,
    member_asset_context: dict | None,
    event_count: int | None,
    path: str,
    require_asset_context: bool = True,
    drop_invalid_entries: bool = False,
) -> list[str]:
    """规范化专用修改数组。

    普通生成与最终消费保持整组严格：任一错误时不部分覆盖原输入。只有
    Observer/Guard 已取得完整数组覆盖权的定向修复出口可启用
    ``drop_invalid_entries``，把无法完整表达的单条修改投影为空，同时保留
    同数组内已经独立通过合同的 sibling；数组本身非法时仍整组拒绝。
    """
    if not isinstance(member_update, dict):
        return [f'{path}: 必须是对象']

    skill_assets = _asset_rows(member_asset_context, 'skill_assets_by_id')
    equipment_assets = _asset_rows(
        member_asset_context, 'equipment_assets_by_id',
    )
    errors: list[str] = []
    replacements: dict[str, list[dict]] = {}

    if S1_SKILL_UPDATES_FIELD in member_update:
        normalized, field_errors = _normalize_skill_updates(
            member_update.get(S1_SKILL_UPDATES_FIELD),
            path=f'{path}.{S1_SKILL_UPDATES_FIELD}',
            assets=skill_assets,
            require_asset_context=require_asset_context,
            event_count=event_count,
        )
        if field_errors and (not drop_invalid_entries or normalized is None):
            errors.extend(field_errors)
        if normalized is not None:
            replacements[S1_SKILL_UPDATES_FIELD] = normalized

    if S1_EQUIPMENT_UPDATES_FIELD in member_update:
        normalized, field_errors = _normalize_equipment_updates(
            member_update.get(S1_EQUIPMENT_UPDATES_FIELD),
            path=f'{path}.{S1_EQUIPMENT_UPDATES_FIELD}',
            assets=equipment_assets,
            require_asset_context=require_asset_context,
            event_count=event_count,
        )
        if field_errors and (not drop_invalid_entries or normalized is None):
            errors.extend(field_errors)
        if normalized is not None:
            replacements[S1_EQUIPMENT_UPDATES_FIELD] = normalized

    if errors:
        return errors

    member_update.update(replacements)
    return []
