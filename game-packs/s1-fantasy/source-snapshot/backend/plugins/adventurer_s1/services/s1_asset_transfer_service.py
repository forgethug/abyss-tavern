"""Exact-instance transfers; never turn another member's asset into a grant."""
from __future__ import annotations
from copy import deepcopy
from datetime import date, datetime
TRANSFER_FIELDS = frozenset({'asset_kind', 'asset_id', 'from_adventurer_id', 'name', 'event_indexes'})

def normalize_received_assets(result, member_ids=None, *, partial=False):
    """Validate the explicit transfer protocol before any member writes."""
    errors, seen = ([], set())
    members = set(member_ids or [row.get('id') for row in result.get('member_updates', []) if isinstance(row, dict)])
    event_count = len(result.get('events') or [])
    for update in result.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        rows = update.get('received_assets', [])
        if not isinstance(rows, list):
            errors.append('received_assets 必须为数组')
            continue
        if rows and False:
            errors.append('当前平衡规则不允许技能或特质转移')
        projected_rows = []
        for row in rows:
            if not isinstance(row, dict):
                errors.append('received_assets 每项必须是对象')
                continue
            row = {key: value for key, value in row.items() if key in TRANSFER_FIELDS}
            if set(row) != TRANSFER_FIELDS:
                errors.append('received_assets 字段不完整或包含未知字段')
                continue
            kind, asset_id, source_id = (row['asset_kind'], row['asset_id'], row['from_adventurer_id'])
            indexes = row['event_indexes']
            if kind not in {'skill', 'trait'} or type(asset_id) is not int or asset_id <= 0 or (type(source_id) is not int) or (source_id <= 0) or (not partial and source_id not in members) or (source_id == update.get('id')) or (not isinstance(row['name'], str)) or (not row['name'].strip()) or (not isinstance(indexes, list)) or (not indexes) or any((type(i) is not int or i < 0 or (not partial and i >= event_count) for i in indexes)):
                errors.append('received_assets 身份、名称或事件索引无效')
                continue
            key = (kind, asset_id)
            if key in seen:
                errors.append('同一资产不能重复转移')
            seen.add(key)
            row['event_indexes'] = list(dict.fromkeys(indexes))
            projected_rows.append(row)
        if not errors:
            update['received_assets'] = projected_rows
    return errors

def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return deepcopy(value)

def asset_snapshot(row):
    """Internal recovery evidence; callers must not project this to players."""
    return {column.name: _json_value(getattr(row, column.name)) for column in row.__table__.columns}

def validate_transfer_dependencies(assets, destinations):
    """Check every live dependency affected by the final ownership plan."""
    final_owners = {(kind, row.id): destinations.get((kind, row.id), row.adventurer_id) for kind, rows in assets.items() for row in rows}
    for trait in assets['trait']:
        if trait.is_archived:
            continue
        key = ('trait', trait.id)
        dependencies = trait.asset_dependencies or []
        if key not in destinations and (not any(((dep.get('asset_kind'), dep.get('asset_id')) in destinations for dep in dependencies))):
            continue
        owner = final_owners[key]
        if any((final_owners.get((dep.get('asset_kind'), dep.get('asset_id'))) != owner for dep in dependencies)):
            raise ValueError('特质依赖资产未一并到达最终持有者')

def apply_asset_transfers(transfers, *, allowed_member_ids, frozen_assets=None):
    """Validate a complete plan, then move the same rows atomically.

    Caller owns commit/rollback. Asset content and absolute recovery timestamps
    survive unchanged; carrying capacity may put a received skill in reserve.
    """
    if not transfers:
        return []
    from flask import current_app
    from db_repository.s1_asset_transfer_repository import lock_transfer_state
    from db_repository.manager import db_manager
    allowed = set(allowed_member_ids)
    requested = {mid for row in transfers for mid in (row['from_adventurer_id'], row['to_adventurer_id'])}
    if not requested.issubset(allowed):
        raise ValueError('资产转移成员不属于本次冻结小队')
    members, assets = lock_transfer_state(requested)
    if set(members) != requested:
        raise ValueError('资产转移成员已不存在')
    by_key = {(kind, row.id): row for kind, rows in assets.items() for row in rows}
    plan, seen = ([], set())
    destinations = {}
    for transfer in transfers:
        key = (transfer['asset_kind'], transfer['asset_id'])
        source, target = (transfer['from_adventurer_id'], transfer['to_adventurer_id'])
        row = by_key.get(key)
        if key in seen or source == target or row is None or (row.adventurer_id != source):
            raise ValueError('转移来源已变化或同一资产重复转移')
        if row.name != transfer['name'] or getattr(row, 'is_archived', False):
            raise ValueError('转移资产身份已变化')
        if getattr(members[target], 'status', None) == 'dead':
            raise ValueError('不能向已死亡成员写入持有资产')
        if frozen_assets is not None:
            frozen = frozen_assets.get((source, *key))
            if not frozen or frozen.get('name') != row.name or str(frozen.get('description') or frozen.get('desc') or '') != row.description:
                raise ValueError('转移来源与冻结资产不一致')
        seen.add(key)
        destinations[key] = target
        plan.append((transfer, row, asset_snapshot(row)))
    final_names = {}
    for kind, rows in assets.items():
        for row in rows:
            owner = destinations.get((kind, row.id), row.adventurer_id)
            name_key = (owner, kind, row.name)
            if name_key in final_names and ((kind, row.id) in destinations or final_names[name_key] in destinations):
                raise ValueError('接收方已有同名资产，不能覆盖或合并转移实例')
            final_names[name_key] = (kind, row.id)
    validate_transfer_dependencies(assets, destinations)
    equipped_counts = {mid: sum((1 for row in assets['skill'] if row.adventurer_id == mid and row.equipped and (('skill', row.id) not in destinations))) for mid in requested}
    receipts = []
    for transfer, row, before in plan:
        target = transfer['to_adventurer_id']
        if transfer['asset_kind'] == 'trait':
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import rebase_transferred_trait_rest
            rebase_transferred_trait_rest(row, members[transfer['from_adventurer_id']], members[target])
        row.adventurer = members[target]
        row.adventurer_id = target
        if transfer['asset_kind'] == 'skill':
            row.equipped = bool(row.equipped and equipped_counts[target] < int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5)))
            equipped_counts[target] += int(row.equipped)
        receipts.append({**transfer, 'before': before, 'after': asset_snapshot(row)})
    success, error = db_manager.flush()
    if not success:
        raise ValueError(f'资产转移写入失败: {error}')
    for receipt, (_, row, _) in zip(receipts, plan):
        receipt['after'] = asset_snapshot(row)
    return receipts

def expedition_asset_transfers(full_log):
    transfers = []
    for update in full_log.get('member_updates') or []:
        for row in update.get('received_assets') or []:
            transfers.append({**row, 'to_adventurer_id': update['id']})
    frozen = {}
    for member in full_log.get('audit_member_snapshot') or []:
        mid = member.get('adventurer_id')
        for kind, field in (('skill', 'skills'), ('trait', 'traits')):
            for row in member.get(field) or []:
                aid = row.get('asset_id', row.get('id'))
                if type(aid) is int:
                    frozen[mid, kind, aid] = row
    return apply_asset_transfers(transfers, allowed_member_ids=[row['id'] for row in full_log.get('member_updates') or []], frozen_assets=frozen)
