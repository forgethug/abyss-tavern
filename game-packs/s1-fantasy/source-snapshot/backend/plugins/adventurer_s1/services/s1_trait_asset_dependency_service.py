"""S1 特质与装备/技能的结构化依赖合同。

依赖以稳定资产 ID 为权威；装备在随身/仓库之间移动会重建实例，因此同时保留
精确名称快照作为跨存储移动后的确定性回退。旧特质只允许按玩家已有资产的完整
名称做精确实体映射，不做关键词、词表或模糊语义推断。
"""
from __future__ import annotations

from collections.abc import Iterable
import re

from flask import current_app
from plugins.adventurer_s1.services.s1_settlement_field_projection import (
    project_s1_trait_fields,
)

from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1Skill,
    AdventurerS1Trait,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


VALID_TRAIT_ASSET_KINDS = frozenset({'equipment', 'skill'})
VALID_EQUIPMENT_LOCATIONS = frozenset({'equipment', 'warehouse', 'missing'})
TRAIT_ASSET_DEPENDENCY_ERROR_MARKER = '特质资产依赖合同错误'


def get_max_trait_asset_dependencies() -> int:
    try:
        configured = current_app.config.get(
            'ADVENTURER_S1_TRAIT_ASSET_DEPENDENCY_MAX', 8,
        )
    except RuntimeError:
        configured = 8
    return max(1, int(configured or 8))


def _clean_name(value: object) -> str:
    return str(value or '').strip()


def _new_asset_names(member_update: dict, *, asset_kind: str) -> set[str]:
    if asset_kind == 'equipment':
        raw = member_update.get('new_equipments')
        if not isinstance(raw, dict):
            return set()
        return {_clean_name(name) for name in raw if _clean_name(name)}

    raw = member_update.get('new_skills')
    names: set[str] = set()
    if isinstance(raw, dict):
        names.update(_clean_name(name) for name in raw if _clean_name(name))
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                name = _clean_name(entry.get('name'))
            else:
                name = _clean_name(entry)
            if name:
                names.add(name)
    return names


def normalize_member_new_trait_asset_dependencies(
    member_update: dict,
    *,
    member_asset_context: dict | None,
    path: str,
) -> list[str]:
    """校验并原位规范化 ``new_traits[*].asset_dependencies``。"""
    raw_traits = member_update.get('new_traits')
    if raw_traits in (None, {}, []):
        return []
    if not isinstance(raw_traits, list):
        return [
            f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: {path}.new_traits '
            '必须是对象数组；每项都要显式提供 asset_dependencies'
        ]

    raw_traits = [project_s1_trait_fields(row) for row in raw_traits]
    member_update['new_traits'] = raw_traits

    context = member_asset_context if isinstance(member_asset_context, dict) else {}
    equipment_by_id = context.get('equipment_assets_by_id') or {}
    skill_by_id = context.get('skill_assets_by_id') or {}
    new_names = {
        'equipment': _new_asset_names(member_update, asset_kind='equipment'),
        'skill': _new_asset_names(member_update, asset_kind='skill'),
    }
    max_dependencies = get_max_trait_asset_dependencies()
    errors: list[str] = []

    for trait_index, trait_entry in enumerate(raw_traits):
        error_count_before_trait = len(errors)
        trait_path = f'{path}.new_traits[{trait_index}]'
        if not isinstance(trait_entry, dict):
            errors.append(
                f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: {trait_path} 必须是对象'
            )
            continue
        if 'asset_dependencies' not in trait_entry:
            errors.append(
                f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                f'{trait_path}.asset_dependencies 缺失；独立特质也必须显式写 []'
            )
            continue
        raw_dependencies = trait_entry.get('asset_dependencies')
        if not isinstance(raw_dependencies, list):
            errors.append(
                f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                f'{trait_path}.asset_dependencies 必须是数组'
            )
            continue
        if len(raw_dependencies) > max_dependencies:
            errors.append(
                f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                f'{trait_path}.asset_dependencies 最多 {max_dependencies} 项'
            )

        normalized: list[dict] = []
        seen: set[tuple[str, str, object]] = set()
        for dependency_index, raw_dependency in enumerate(
            raw_dependencies[:max_dependencies]
        ):
            dependency_path = (
                f'{trait_path}.asset_dependencies[{dependency_index}]'
            )
            if not isinstance(raw_dependency, dict):
                errors.append(
                    f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                    f'{dependency_path} 必须是对象'
                )
                continue
            extra_keys = set(raw_dependency) - {
                'asset_kind', 'asset_id', 'new_asset_name',
            }
            if extra_keys:
                errors.append(
                    f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                    f'{dependency_path} 含未授权字段 {sorted(extra_keys)}'
                )
                continue
            asset_kind = _clean_name(raw_dependency.get('asset_kind')).lower()
            if asset_kind not in VALID_TRAIT_ASSET_KINDS:
                errors.append(
                    f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                    f'{dependency_path}.asset_kind 只能是 equipment 或 skill'
                )
                continue
            has_asset_id = raw_dependency.get('asset_id') is not None
            new_asset_name = _clean_name(raw_dependency.get('new_asset_name'))
            if has_asset_id == bool(new_asset_name):
                errors.append(
                    f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: {dependency_path} '
                    '必须且只能填写 asset_id / new_asset_name 之一'
                )
                continue
            if has_asset_id:
                asset_id, _ = normalize_ascii_integer(
                    raw_dependency.get('asset_id'), minimum=1,
                )
                candidates = (
                    equipment_by_id if asset_kind == 'equipment' else skill_by_id
                )
                if type(asset_id) is not int or asset_id not in candidates:
                    errors.append(
                        f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                        f'{dependency_path}.asset_id 不在该成员本次出发的'
                        f'{"装备" if asset_kind == "equipment" else "已携带技能"}白名单'
                    )
                    continue
                identity = (asset_kind, 'id', asset_id)
                entry = {'asset_kind': asset_kind, 'asset_id': asset_id}
            else:
                if new_asset_name not in new_names[asset_kind]:
                    errors.append(
                        f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                        f'{dependency_path}.new_asset_name 必须精确来自本成员同次 '
                        f'{"new_equipments" if asset_kind == "equipment" else "new_skills"}'
                    )
                    continue
                identity = (asset_kind, 'new_name', new_asset_name)
                entry = {
                    'asset_kind': asset_kind,
                    'new_asset_name': new_asset_name,
                }
            if identity in seen:
                errors.append(
                    f'{TRAIT_ASSET_DEPENDENCY_ERROR_MARKER}: '
                    f'{dependency_path} 重复引用同一资产'
                )
                continue
            seen.add(identity)
            normalized.append(entry)
        if len(errors) == error_count_before_trait:
            trait_entry['asset_dependencies'] = normalized
    return errors


def normalize_persisted_trait_asset_dependencies(raw: object) -> list[dict]:
    """读取持久化依赖；非法单项独立丢弃，不把脏 JSON 扩大为授权。"""
    if not isinstance(raw, list):
        return []
    normalized: list[dict] = []
    seen: set[tuple[str, int | None, str, str]] = set()
    for entry in raw[:get_max_trait_asset_dependencies()]:
        if not isinstance(entry, dict):
            continue
        asset_kind = _clean_name(entry.get('asset_kind')).lower()
        if asset_kind not in VALID_TRAIT_ASSET_KINDS:
            continue
        asset_id, _ = normalize_ascii_integer(entry.get('asset_id'), minimum=1)
        if type(asset_id) is not int:
            asset_id = None
        asset_name = _clean_name(entry.get('asset_name'))
        if not asset_name:
            continue
        if asset_kind == 'equipment':
            location = _clean_name(entry.get('asset_location')).lower()
            if location not in VALID_EQUIPMENT_LOCATIONS:
                location = 'equipment'
        else:
            location = 'skill'
        identity = (asset_kind, asset_id, location, asset_name)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({
            'asset_kind': asset_kind,
            'asset_id': asset_id,
            'asset_location': location,
            'asset_name': asset_name,
        })
    return normalized


def resolve_trait_asset_dependencies_for_write(
    raw_dependencies: object,
    *,
    adventurer_id: int,
    equipment_items: Iterable[object],
    skill_items: Iterable[object],
    frozen_member_snapshot: dict | None = None,
) -> list[dict]:
    """把已校验的生成侧引用解析为可持久化的 ID + 名称快照。"""
    raw_rows = raw_dependencies if isinstance(raw_dependencies, list) else []
    equipment_rows = list(equipment_items or [])
    skill_rows = list(skill_items or [])
    requested_equipment_names = {
        _clean_name(row.get('new_asset_name'))
        for row in raw_rows
        if isinstance(row, dict)
        and _clean_name(row.get('asset_kind')).lower() == 'equipment'
        and _clean_name(row.get('new_asset_name'))
    }
    requested_skill_names = {
        _clean_name(row.get('new_asset_name'))
        for row in raw_rows
        if isinstance(row, dict)
        and _clean_name(row.get('asset_kind')).lower() == 'skill'
        and _clean_name(row.get('new_asset_name'))
    }
    if requested_equipment_names:
        known_ids = {
            int(row.id) for row in equipment_rows
            if getattr(row, 'id', None) is not None
        }
        equipment_rows.extend(
            row for row in AdventurerS1Equipment.query.filter(
                AdventurerS1Equipment.adventurer_id == adventurer_id,
                AdventurerS1Equipment.name.in_(sorted(requested_equipment_names)),
            ).order_by(AdventurerS1Equipment.id.asc()).all()
            if int(row.id) not in known_ids
        )
    if requested_skill_names:
        known_ids = {
            int(row.id) for row in skill_rows
            if getattr(row, 'id', None) is not None
        }
        skill_rows.extend(
            row for row in AdventurerS1Skill.query.filter(
                AdventurerS1Skill.adventurer_id == adventurer_id,
                AdventurerS1Skill.name.in_(sorted(requested_skill_names)),
            ).order_by(AdventurerS1Skill.id.asc()).all()
            if int(row.id) not in known_ids
        )
    equipment_by_id = {
        int(row.id): row for row in equipment_rows
        if getattr(row, 'id', None) is not None
    }
    skill_by_id = {
        int(row.id): row for row in skill_rows
        if getattr(row, 'id', None) is not None
    }
    warehouse_rows = (
        AdventurerS1WarehouseItem.query.filter(
            AdventurerS1WarehouseItem.adventurer_id == adventurer_id,
            AdventurerS1WarehouseItem.name.in_(sorted(requested_equipment_names)),
        ).order_by(AdventurerS1WarehouseItem.id.asc()).all()
        if requested_equipment_names else []
    )
    frozen = frozen_member_snapshot if isinstance(frozen_member_snapshot, dict) else {}
    frozen_equipment = {
        int(row.get('asset_id')): row
        for row in frozen.get('equipment') or []
        if isinstance(row, dict) and type(row.get('asset_id')) is int
    }
    frozen_skills = {
        int(row.get('asset_id')): row
        for row in frozen.get('skills') or []
        if isinstance(row, dict) and type(row.get('asset_id')) is int
    }

    resolved: list[dict] = []
    seen: set[tuple[str, int | None, str]] = set()
    for raw in raw_rows[:get_max_trait_asset_dependencies()]:
        if not isinstance(raw, dict):
            continue
        asset_kind = _clean_name(raw.get('asset_kind')).lower()
        if asset_kind not in VALID_TRAIT_ASSET_KINDS:
            continue
        asset_id, _ = normalize_ascii_integer(raw.get('asset_id'), minimum=1)
        new_asset_name = _clean_name(raw.get('new_asset_name'))
        asset_name = ''
        location = 'skill' if asset_kind == 'skill' else 'missing'
        resolved_id: int | None = None

        if type(asset_id) is int:
            if asset_kind == 'equipment':
                current = equipment_by_id.get(asset_id)
                snapshot = frozen_equipment.get(asset_id)
                asset_name = _clean_name(
                    getattr(current, 'name', None)
                    or (snapshot or {}).get('name')
                )
                resolved_id = asset_id
                location = 'equipment'
            else:
                current = skill_by_id.get(asset_id)
                snapshot = frozen_skills.get(asset_id)
                asset_name = _clean_name(
                    getattr(current, 'name', None)
                    or (snapshot or {}).get('name')
                )
                resolved_id = asset_id
        elif new_asset_name:
            asset_name = new_asset_name
            if asset_kind == 'equipment':
                current = next(
                    (row for row in equipment_rows if _clean_name(row.name) == new_asset_name),
                    None,
                )
                if current is not None:
                    resolved_id = int(current.id)
                    location = 'equipment'
                else:
                    stored = next(
                        (row for row in warehouse_rows if _clean_name(row.name) == new_asset_name),
                        None,
                    )
                    if stored is not None:
                        resolved_id = int(stored.id)
                        location = 'warehouse'
            else:
                current = next(
                    (row for row in skill_rows if _clean_name(row.name) == new_asset_name),
                    None,
                )
                if current is not None:
                    resolved_id = int(current.id)
        if not asset_name:
            continue
        identity = (asset_kind, resolved_id, asset_name)
        if identity in seen:
            continue
        seen.add(identity)
        resolved.append({
            'asset_kind': asset_kind,
            'asset_id': resolved_id,
            'asset_location': location,
            'asset_name': asset_name,
        })
    return resolved


def _legacy_exact_dependencies(
    trait,
    *,
    equipment_items: Iterable[object],
    warehouse_items: Iterable[object],
    skill_items: Iterable[object],
) -> list[dict]:
    description = _clean_name(getattr(trait, 'description', None))
    if not description:
        return []
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for location, rows in (
        ('equipment', equipment_items),
        ('warehouse', warehouse_items),
    ):
        for row in rows or []:
            name = _clean_name(getattr(row, 'name', None))
            identity = ('equipment', name)
            if not name or name not in description or identity in seen:
                continue
            seen.add(identity)
            candidates.append({
                'asset_kind': 'equipment',
                'asset_id': int(row.id) if getattr(row, 'id', None) is not None else None,
                'asset_location': location,
                'asset_name': name,
            })
    for row in skill_items or []:
        name = _clean_name(getattr(row, 'name', None))
        identity = ('skill', name)
        if not name or name not in description or identity in seen:
            continue
        seen.add(identity)
        candidates.append({
            'asset_kind': 'skill',
            'asset_id': int(row.id) if getattr(row, 'id', None) is not None else None,
            'asset_location': 'skill',
            'asset_name': name,
        })
    return candidates[:get_max_trait_asset_dependencies()]


def trait_is_active_for_expedition(
    trait,
    *,
    equipment_items: Iterable[object],
    skill_items: Iterable[object],
    warehouse_items: Iterable[object] = (),
) -> bool:
    """全部声明依赖都在本次随身/已装备白名单中时，特质才可注入 AI。"""
    raw = getattr(trait, 'asset_dependencies', None)
    dependencies = normalize_persisted_trait_asset_dependencies(raw)
    if raw is None:
        dependencies = _legacy_exact_dependencies(
            trait,
            equipment_items=equipment_items,
            warehouse_items=warehouse_items,
            skill_items=skill_items,
        )
    elif (
        not isinstance(raw, list)
        or len(raw) > get_max_trait_asset_dependencies()
        or len(dependencies) != len(raw)
        or any(
            not isinstance(entry, dict)
            or _clean_name(entry.get('asset_kind')).lower()
            not in VALID_TRAIT_ASSET_KINDS
            or not _clean_name(entry.get('asset_name'))
            or (
                _clean_name(entry.get('asset_kind')).lower() == 'equipment'
                and _clean_name(entry.get('asset_location')).lower()
                not in VALID_EQUIPMENT_LOCATIONS
            )
            or (
                _clean_name(entry.get('asset_kind')).lower() == 'skill'
                and _clean_name(entry.get('asset_location')).lower() != 'skill'
            )
            for entry in raw
        )
    ):
        # 已声明依赖一旦损坏，必须按不生效处理，不能把脏数据降级成独立特质。
        return False
    if not dependencies:
        return True

    equipment_rows = list(equipment_items or [])
    skill_rows = list(skill_items or [])
    carried_equipment_ids = {
        int(row.id) for row in equipment_rows if getattr(row, 'id', None) is not None
    }
    carried_equipment_names = {
        _clean_name(getattr(row, 'name', None)) for row in equipment_rows
        if _clean_name(getattr(row, 'name', None))
    }
    equipped_skill_ids = {
        int(row.id) for row in skill_rows
        if getattr(row, 'id', None) is not None
        and bool(getattr(row, 'equipped', True))
    }
    equipped_skill_names = {
        _clean_name(getattr(row, 'name', None)) for row in skill_rows
        if bool(getattr(row, 'equipped', True))
        and _clean_name(getattr(row, 'name', None))
    }
    for dependency in dependencies:
        asset_id = dependency.get('asset_id')
        asset_name = dependency['asset_name']
        if dependency['asset_kind'] == 'equipment':
            if asset_id in carried_equipment_ids:
                continue
            if asset_name in carried_equipment_names:
                continue
            return False
        if asset_id in equipped_skill_ids:
            continue
        if asset_name in equipped_skill_names:
            continue
        return False
    return True


def trait_asset_dependency_states_for_members(
    members,
) -> tuple[set[int], set[int]]:
    """批量返回 ``(active_ids, dependency_ids)``，旧仓库最多查询一次。"""
    member_rows = list(members or [])
    member_ids = {
        int(member.id) for member in member_rows if getattr(member, 'id', None) is not None
    }
    needs_legacy_warehouse = any(
        getattr(trait, 'asset_dependencies', None) is None
        for member in member_rows
        for trait in (getattr(member, 'traits', None) or [])
        if not bool(getattr(trait, 'is_archived', False))
    )
    warehouse_by_adventurer: dict[int, list] = {member_id: [] for member_id in member_ids}
    if member_ids and needs_legacy_warehouse:
        for row in AdventurerS1WarehouseItem.query.filter(
            AdventurerS1WarehouseItem.adventurer_id.in_(sorted(member_ids)),
        ).all():
            warehouse_by_adventurer.setdefault(int(row.adventurer_id), []).append(row)

    active_ids: set[int] = set()
    dependency_ids: set[int] = set()
    for member in member_rows:
        member_id = int(member.id)
        equipment = getattr(member, 'equipment_items', None) or []
        skills = getattr(member, 'skill_items', None) or []
        warehouses = warehouse_by_adventurer.get(member_id, [])
        for trait in getattr(member, 'traits', None) or []:
            if bool(getattr(trait, 'is_archived', False)):
                continue
            trait_id = getattr(trait, 'id', None)
            if trait_id is None:
                continue
            raw_dependencies = getattr(trait, 'asset_dependencies', None)
            dependencies = normalize_persisted_trait_asset_dependencies(
                raw_dependencies,
            )
            if raw_dependencies is None:
                dependencies = _legacy_exact_dependencies(
                    trait,
                    equipment_items=equipment,
                    warehouse_items=warehouses,
                    skill_items=skills,
                )
            if dependencies:
                dependency_ids.add(int(trait_id))
            if trait_is_active_for_expedition(
                trait,
                equipment_items=equipment,
                skill_items=skills,
                warehouse_items=warehouses,
            ):
                active_ids.add(int(trait_id))
    return active_ids, dependency_ids


def active_trait_ids_for_members(members) -> set[int]:
    """批量计算本次可注入的特质 ID；兼容既有调用方。"""
    active_ids, _dependency_ids = trait_asset_dependency_states_for_members(
        members,
    )
    return active_ids


def _owned_assets_for_rename(adventurer_id: int) -> list[dict]:
    """批量读取本人的完整资产名，保护未声明依赖的长名正文引用。"""
    from db_repository import db_manager

    assets = []
    for model, kind, location in (
        (AdventurerS1Skill, 'skill', 'skill'),
        (AdventurerS1Equipment, 'equipment', 'equipment'),
        (AdventurerS1WarehouseItem, 'equipment', 'warehouse'),
    ):
        assets.extend(
            {'asset_kind': kind, 'asset_location': location,
             'asset_id': row[0], 'asset_name': _clean_name(row[1])}
            for row in db_manager.query(model.id, model.name).filter(
                model.adventurer_id == adventurer_id,
            ).all()
            if _clean_name(row[1])
        )
    return assets


def sync_trait_asset_renames(
    adventurer_id: int,
    rename_changes: Iterable[dict],
    *,
    traits: Iterable[AdventurerS1Trait] | None = None,
) -> int:
    """同事务同步依赖快照与特质当前正文；追加式履历保持历史原文。"""
    changes = []
    for raw in rename_changes or []:
        if not isinstance(raw, dict):
            continue
        kind = _clean_name(raw.get('asset_kind')).lower()
        old_name = _clean_name(raw.get('old_name'))
        new_name = _clean_name(raw.get('new_name'))
        asset_id, _ = normalize_ascii_integer(raw.get('asset_id'), minimum=1)
        if (
            kind not in VALID_TRAIT_ASSET_KINDS
            or type(asset_id) is not int
            or not old_name
            or not new_name
            or old_name == new_name
        ):
            continue
        location = (
            _clean_name(raw.get('asset_location')).lower()
            if kind == 'equipment' else 'skill'
        )
        if kind == 'equipment' and location not in VALID_EQUIPMENT_LOCATIONS:
            location = 'equipment'
        changes.append({
            'asset_kind': kind,
            'asset_id': asset_id,
            'asset_location': location,
            'old_name': old_name,
            'new_name': new_name,
        })
    if not changes:
        return 0

    trait_rows = list(traits) if traits is not None else (
        AdventurerS1Trait.query.filter_by(
            adventurer_id=adventurer_id,
            is_archived=False,
        ).order_by(AdventurerS1Trait.id.asc()).populate_existing().with_for_update().all()
    )
    owned_assets: list[dict] | None = None
    renamed_identities = {
        (change['asset_kind'], change['asset_location'], change['asset_id'])
        for change in changes
    }

    changed_count = 0
    for trait in trait_rows:
        raw_dependencies = getattr(trait, 'asset_dependencies', None)
        dependencies = normalize_persisted_trait_asset_dependencies(raw_dependencies)
        description = _clean_name(getattr(trait, 'description', None))
        if raw_dependencies is None:
            # 旧正文出现名称子串不能证明依赖身份。改名只迁移已有绑定，
            # 不在这里凭正文建立新绑定，否则短名会污染另一个长名技能。
            continue
        if not dependencies:
            continue

        original_names = [dependency['asset_name'] for dependency in dependencies]
        replacements: list[tuple[str, str]] = []
        dependency_changed = False
        for dependency in dependencies:
            for change in changes:
                if dependency['asset_kind'] != change['asset_kind']:
                    continue
                id_match = (
                    dependency.get('asset_id') == change['asset_id']
                    and (
                        dependency['asset_kind'] == 'skill'
                        or dependency.get('asset_location')
                        == change['asset_location']
                    )
                )
                if not id_match:
                    continue
                old_name = dependency['asset_name']
                dependency['asset_id'] = change['asset_id']
                dependency['asset_location'] = change['asset_location']
                dependency['asset_name'] = change['new_name']
                dependency_changed = True
                if old_name and old_name != change['new_name']:
                    replacements.append((old_name, change['new_name']))
                break
        if not dependency_changed:
            continue

        trait.asset_dependencies = dependencies
        # 同一正文中的完整长名优先；一次替换，避免 A→B、B→C 串成 A→C。
        # 同名但不同实例映射到不同结果时，没有可确定的正文替换权限。
        destinations: dict[str, set[str]] = {name: set() for name in original_names}
        for old_name, dependency in zip(original_names, dependencies):
            destinations[old_name].add(dependency['asset_name'])
        if replacements:
            if owned_assets is None:
                owned_assets = _owned_assets_for_rename(adventurer_id)
            for asset in owned_assets:
                name = asset['asset_name']
                destinations.setdefault(name, {name})
                identity = (asset['asset_kind'], asset['asset_location'], asset['asset_id'])
                if identity not in renamed_identities:
                    destinations[name].add(name)
            for change in changes:
                if change['old_name'] in original_names:
                    # 同批另一同名实例也可能改向不同名称；它只能阻止
                    # 歧义正文同步，不能给未绑定名称新增替换权限。
                    destinations[change['old_name']].add(change['new_name'])
                for name in (change['old_name'], change['new_name']):
                    destinations.setdefault(name, {name})
        name_pattern = re.compile('|'.join(
            re.escape(name) for name in sorted(destinations, key=lambda name: (-len(name), name))
        ))

        def _transform(text: str) -> str:
            def _replace(match: re.Match) -> str:
                targets = destinations[match.group(0)]
                return next(iter(targets)) if len(targets) == 1 else match.group(0)
            return name_pattern.sub(_replace, text)

        recovered_desc = _clean_name(getattr(trait, 'recovered_desc', None))
        if recovered_desc and replacements:
            transformed_recovered_desc = _transform(recovered_desc)
            if transformed_recovered_desc != recovered_desc:
                trait.recovered_desc = transformed_recovered_desc
        if replacements:
            new_description = _transform(description)
            if new_description != description:
                from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
                    apply_deterministic_trait_description_transform,
                )
                apply_deterministic_trait_description_transform(
                    trait,
                    new_description,
                    transform=_transform,
                )
        changed_count += 1
    return changed_count
