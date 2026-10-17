"""显式管理员资产机制修订；不从名称或描述自动判定是否需要削弱。

只消费人工核验的精确版本清单。写入前锁定角色及资产，完整前后文与执行者在同一
事务留档；历史探险快照和结算不变。普通探险不调用本服务。
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from copy import deepcopy
from datetime import datetime
import hashlib
import json

from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1DataAppeal,
    AdventurerS1DeploymentIdentity,
    AdventurerS1Equipment,
    AdventurerS1Settings,
    AdventurerS1Skill,
    AdventurerS1Trait,
)

_ASSETS = {
    'equipment': (AdventurerS1Equipment, 'custom_desc'),
    'skill': (AdventurerS1Skill, 'description'),
    'trait': (AdventurerS1Trait, 'description'),
}


def description_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def validate_revision_manifest(manifest: object) -> dict:
    """仅校验结构与版本锚点，经济机制是否合法由管理员逐项确认。"""
    if not isinstance(manifest, dict) or set(manifest) != {
        'schema_version', 'realm_code', 'deployment_id', 'items',
    }:
        raise ValueError('修订清单字段不完整或包含未支持字段')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
        raise ValueError('不支持的修订清单版本')
    if not isinstance(manifest['realm_code'], str) or manifest['realm_code'] not in {'region_1', 'region_2'}:
        raise ValueError('区服无效')
    if not isinstance(manifest['deployment_id'], str) or not manifest['deployment_id'].strip():
        raise ValueError('缺少部署身份')
    items = manifest['items']
    if not isinstance(items, list) or not items:
        raise ValueError('修订清单不能为空')
    limit = current_app.config['ADVENTURER_S1_ASSET_MECHANISM_REVISION_MAX_ITEMS']
    if type(limit) is not int or limit <= 0 or len(items) > limit:
        raise ValueError('修订清单超过已配置的单批上限')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            'asset_kind', 'asset_id', 'adventurer_id', 'name',
            'before_description', 'before_hash', 'after_description',
            'reason', 'evidence_log_ids',
        }:
            raise ValueError('资产修订字段不完整或包含未支持字段')
        if not isinstance(item['asset_kind'], str) or item['asset_kind'] not in _ASSETS:
            raise ValueError('只支持装备、技能与特质实例')
        for field in ('asset_id', 'adventurer_id'):
            if type(item[field]) is not int or item[field] <= 0:
                raise ValueError('必须提供精确的正整数资产与角色 ID')
        for field in ('name', 'before_description', 'after_description', 'reason'):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ValueError(f'{field} 必须是非空完整文本')
        if item['after_description'] != item['after_description'].strip():
            raise ValueError('修订正文首尾不能有空白')
        if item['before_hash'] != description_hash(item['before_description']):
            raise ValueError('原文哈希不匹配')
        if item['after_description'] == item['before_description'].strip():
            raise ValueError('修订前后没有变化')
        evidence = item['evidence_log_ids']
        if not isinstance(evidence, list) or not evidence or any(
            type(value) is not int or value <= 0 for value in evidence
        ):
            raise ValueError('缺少已核验的日志 ID')
        key = (item['asset_kind'], item['asset_id'])
        if key in seen:
            raise ValueError('同一资产不能重复修订')
        seen.add(key)
    return deepcopy(manifest)


def _manifest_key(manifest: dict) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    # Settings.key 最长 50；完整摘要同时留在回执中校验，不靠截断摘要认定相同批次。
    return 'asset_revision:' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]


def _assert_identity(manifest: dict) -> None:
    identity = db_manager.query(AdventurerS1DeploymentIdentity).filter_by(id=1).first()
    if identity is None or (
        identity.realm_code != manifest['realm_code']
        or identity.deployment_id != manifest['deployment_id']
    ):
        raise ValueError('数据库区服或部署身份与修订清单不一致')


def _load_targets(manifest: dict, *, lock: bool) -> tuple[dict, dict, set]:
    adventurer_ids = sorted({item['adventurer_id'] for item in manifest['items']})
    query = db_manager.query(AdventurerS1).filter(
        AdventurerS1.id.in_(adventurer_ids),
    ).order_by(AdventurerS1.id)
    if lock:
        query = query.populate_existing().with_for_update()
    adventurers = {row.id: row for row in query.all()}
    targets = {}
    for kind, (model, _) in _ASSETS.items():
        ids = sorted({item['asset_id'] for item in manifest['items'] if item['asset_kind'] == kind})
        if not ids:
            continue
        query = db_manager.query(model).filter(model.id.in_(ids)).order_by(model.id)
        if lock:
            query = query.populate_existing().with_for_update()
        targets.update({(kind, row.id): row for row in query.all()})
    pending_appeals = {
        row[0] for row in db_manager.query(AdventurerS1DataAppeal.adventurer_id).filter(
            AdventurerS1DataAppeal.adventurer_id.in_(adventurer_ids),
            or_(
                AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')),
                AdventurerS1DataAppeal.player_review_status == 'pending',
                AdventurerS1DataAppeal.player_revert_request_status.in_(('processing', 'allowed')),
                AdventurerS1DataAppeal.revert_offer_status == 'pending',
            ),
        ).distinct().all()
    }
    return adventurers, targets, pending_appeals


def _availability(item: dict, manifest: dict, adventurers: dict, targets: dict, pending: set) -> str:
    adventurer = adventurers.get(item['adventurer_id'])
    row = targets.get((item['asset_kind'], item['asset_id']))
    if adventurer is None or adventurer.realm_code != manifest['realm_code'] or row is None:
        return 'stale_target'
    if row.adventurer_id != item['adventurer_id'] or row.name != item['name']:
        return 'stale_target'
    # 等角色结束已冻结输入的任务；不改动进行中的剧情、建筑或申诉。
    if (
        adventurer.status not in {'idle', 'resting'}
        or adventurer.building_instance_id is not None
        or adventurer.skill_training_retry
        or item['adventurer_id'] in pending
    ):
        return 'busy'
    return 'ready'


def _write_description(row, kind: str, description: str) -> None:
    if kind == 'trait':
        from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
            sync_trait_description_fields,
        )
        sync_trait_description_fields(row, description)
        # 条件恢复保留管理员核验的原文格式，不用同步器的 strip 改写历史前值。
        row.description = description
    else:
        setattr(row, _ASSETS[kind][1], description)


def _asset_state(row) -> dict:
    """内部恢复回执保留完整实例状态；派生摘要预热不算资产版本变化。"""
    state = {}
    for column in row.__table__.columns:
        if column.name in {
            'created_at', 'updated_at', 'description_ai',
            'description_ai_at', 'description_ai_source_hash',
        }:
            continue
        value = getattr(row, column.name)
        state[column.name] = value.isoformat() if isinstance(value, datetime) else deepcopy(value)
    return state


def preview_asset_mechanism_revisions(manifest: object) -> dict:
    """只读当前精确原文；无缓存写入、模型调用或自动平衡判断。"""
    manifest = validate_revision_manifest(manifest)
    try:
        _assert_identity(manifest)
        adventurers, targets, pending = _load_targets(manifest, lock=False)
        items = []
        for item in manifest['items']:
            status = _availability(item, manifest, adventurers, targets, pending)
            row = targets.get((item['asset_kind'], item['asset_id']))
            current = getattr(row, _ASSETS[item['asset_kind']][1]) if row is not None else None
            if status == 'ready' and current != item['before_description']:
                status = 'stale_description'
            items.append({
                **item, 'status': status,
                'current_hash': description_hash(current) if isinstance(current, str) else None,
                'after_hash': description_hash(item['after_description']),
                'trait_equipped_after': True if item['asset_kind'] == 'trait' else None,
            })
        return {'receipt_key': _manifest_key(manifest), 'items': items}
    finally:
        db_manager.remove()


def apply_asset_mechanism_revisions(
    manifest: object, *, admin_user_id: int, restore: bool = False,
) -> dict:
    """精确比较后原子改写并留档；同批可重入，回滚只恢复仍匹配本批结果的资产。"""
    manifest = validate_revision_manifest(manifest)
    if type(admin_user_id) is not int or admin_user_id <= 0:
        raise ValueError('缺少管理员 ID')
    key = _manifest_key(manifest)
    changed_adventurers = set()
    results = []
    try:
        with db_manager.transaction():
            _assert_identity(manifest)
            adventurers, targets, pending = _load_targets(manifest, lock=True)
            setting = db_manager.query(AdventurerS1Settings).filter_by(
                key=key,
            ).populate_existing().with_for_update().first()
            receipt = json.loads(setting.value) if setting is not None else {
                'manifest': manifest, 'entries': {},
            }
            if receipt.get('manifest') != manifest:
                raise ValueError('修订回执与清单不一致')
            if restore and setting is None:
                raise ValueError('本批修订没有持久化回执')
            entries = receipt['entries']
            for item in manifest['items']:
                kind = item['asset_kind']
                ref = f"{kind}:{item['asset_id']}"
                entry = entries.get(ref)
                status = _availability(item, manifest, adventurers, targets, pending)
                if not restore and entry:
                    status = 'already_restored' if entry.get('restored_at') else 'already_applied'
                elif restore and (not entry or entry.get('restored_at')):
                    status = 'not_applied' if not entry else 'already_restored'
                elif status == 'ready':
                    row = targets[(kind, item['asset_id'])]
                    expected = item['after_description'] if restore else item['before_description']
                    if getattr(row, _ASSETS[kind][1]) != expected:
                        status = 'stale_description'
                    elif restore and _asset_state(row) != entry['after_state']:
                        status = 'stale_asset_state'
                    else:
                        before_state = _asset_state(row)
                        before_equipped = row.equipped if kind == 'trait' else None
                        description = item['before_description'] if restore else item['after_description']
                        _write_description(row, kind, description)
                        now = _portable_utc_now().isoformat() + 'Z'
                        if restore:
                            if kind == 'trait':
                                row.equipped = entry['trait_equipped_before']
                            entry.update(restored_at=now, restored_by=admin_user_id)
                            status = 'restored'
                        else:
                            entries[ref] = {
                                'applied_at': now, 'applied_by': admin_user_id,
                                'before_state': before_state, 'after_state': _asset_state(row),
                                'trait_equipped_before': before_equipped,
                                'trait_equipped_after': row.equipped if kind == 'trait' else None,
                            }
                            status = 'applied'
                        changed_adventurers.add(item['adventurer_id'])
                results.append({'asset_kind': kind, 'asset_id': item['asset_id'], 'status': status})
            if changed_adventurers:
                if setting is None:
                    setting = AdventurerS1Settings(
                        key=key, description='管理员资产机制修订原文及条件恢复回执',
                    )
                    db_manager.add(setting)
                setting.value = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
                setting.updated_by_user_id = admin_user_id
    finally:
        db_manager.remove()
    # 原文变化自然更换统一摘要哈希，不删除历史摘要或冻结日志。
    from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_adventurer_detail_cache
    for adventurer_id in sorted(changed_adventurers):
        invalidate_adventurer_detail_cache(adventurer_id)
    return {'receipt_key': key, 'items': results}
