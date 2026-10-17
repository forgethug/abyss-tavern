"""主探险 AI 的请求内资产引用。

玩家资产的数据库主键只在后端内部流转。主探险模型只接收一次请求内的
``S1`` / ``E1`` 引用；结果进入既有校验与结算链前，再按同一冻结成员快照
机械还原为真实主键。
"""
from __future__ import annotations


def build_s1_ai_asset_reference_context(
    skill_items,
    equipment_items,
) -> dict[str, dict]:
    """按输入顺序签发请求内引用，并保留双向精确映射。"""
    skill_ids_by_ref: dict[str, int] = {}
    skill_refs_by_id: dict[int, str] = {}
    equipment_ids_by_ref: dict[str, int] = {}
    equipment_refs_by_id: dict[int, str] = {}

    for item in skill_items or []:
        raw_id = getattr(item, 'id', None)
        name = str(getattr(item, 'name', '') or '').strip()
        if raw_id is None or not name:
            continue
        asset_id = int(raw_id)
        asset_ref = f'S{len(skill_ids_by_ref) + 1}'
        skill_ids_by_ref[asset_ref] = asset_id
        skill_refs_by_id[asset_id] = asset_ref

    for item in equipment_items or []:
        raw_id = getattr(item, 'id', None)
        name = str(getattr(item, 'name', '') or '').strip()
        if raw_id is None or not name:
            continue
        asset_id = int(raw_id)
        asset_ref = f'E{len(equipment_ids_by_ref) + 1}'
        equipment_ids_by_ref[asset_ref] = asset_id
        equipment_refs_by_id[asset_id] = asset_ref

    return {
        'skill_asset_ids_by_ref': skill_ids_by_ref,
        'skill_asset_refs_by_id': skill_refs_by_id,
        'equipment_asset_ids_by_ref': equipment_ids_by_ref,
        'equipment_asset_refs_by_id': equipment_refs_by_id,
    }


def _resolve_entry_reference(
    entry: dict,
    *,
    ref_field: str,
    id_field: str,
    ids_by_ref: dict,
    path: str,
) -> list[str]:
    if ref_field not in entry:
        # 兼容部署时已经在途、仍按旧 Prompt 返回内部 ID 的任务；新 Prompt 不再
        # 注入这些 ID。既有下游白名单仍会验证旧字段确实属于该成员。
        return []
    if id_field in entry:
        return [f'{path}: {ref_field} 与 {id_field} 不得同时出现']

    asset_ref = entry.get(ref_field)
    if not isinstance(asset_ref, str) or asset_ref != asset_ref.strip():
        return [f'{path}.{ref_field}: 必须精确复制本次请求内引用']
    asset_id = ids_by_ref.get(asset_ref)
    if type(asset_id) is not int or asset_id <= 0:
        return [f'{path}.{ref_field}: 不属于该成员本次请求的资产引用表']

    entry.pop(ref_field, None)
    entry[id_field] = asset_id
    return []


def resolve_s1_ai_asset_references(
    member_update: dict,
    *,
    member_asset_context: dict | None,
    path: str,
) -> list[str]:
    """把模型返回的请求内引用原位还原为后端稳定资产 ID。"""
    if not isinstance(member_update, dict):
        return [f'{path}: 必须是对象']
    context = member_asset_context if isinstance(member_asset_context, dict) else {}
    skill_ids_by_ref = context.get('skill_asset_ids_by_ref') or {}
    equipment_ids_by_ref = context.get('equipment_asset_ids_by_ref') or {}
    errors: list[str] = []

    for index, entry in enumerate(member_update.get('skill_updates') or []):
        if isinstance(entry, dict):
            errors.extend(_resolve_entry_reference(
                entry,
                ref_field='skill_ref',
                id_field='skill_id',
                ids_by_ref=skill_ids_by_ref,
                path=f'{path}.skill_updates[{index}]',
            ))

    for index, entry in enumerate(member_update.get('equipment_updates') or []):
        if isinstance(entry, dict):
            errors.extend(_resolve_entry_reference(
                entry,
                ref_field='equipment_ref',
                id_field='equipment_id',
                ids_by_ref=equipment_ids_by_ref,
                path=f'{path}.equipment_updates[{index}]',
            ))

    for index, entry in enumerate(member_update.get('recovery_triggers') or []):
        if not isinstance(entry, dict) or 'asset_ref' not in entry:
            continue
        asset_kind = entry.get('asset_kind')
        ids_by_ref = (
            skill_ids_by_ref if asset_kind == 'skill'
            else equipment_ids_by_ref if asset_kind == 'equipment'
            else {}
        )
        errors.extend(_resolve_entry_reference(
            entry,
            ref_field='asset_ref',
            id_field='asset_id',
            ids_by_ref=ids_by_ref,
            path=f'{path}.recovery_triggers[{index}]',
        ))

    for trait_index, trait in enumerate(member_update.get('new_traits') or []):
        if not isinstance(trait, dict):
            continue
        for dependency_index, dependency in enumerate(
            trait.get('asset_dependencies') or []
        ):
            if not isinstance(dependency, dict) or 'asset_ref' not in dependency:
                continue
            asset_kind = dependency.get('asset_kind')
            ids_by_ref = (
                skill_ids_by_ref if asset_kind == 'skill'
                else equipment_ids_by_ref if asset_kind == 'equipment'
                else {}
            )
            errors.extend(_resolve_entry_reference(
                dependency,
                ref_field='asset_ref',
                id_field='asset_id',
                ids_by_ref=ids_by_ref,
                path=(
                    f'{path}.new_traits[{trait_index}]'
                    f'.asset_dependencies[{dependency_index}]'
                ),
            ))

    return errors
