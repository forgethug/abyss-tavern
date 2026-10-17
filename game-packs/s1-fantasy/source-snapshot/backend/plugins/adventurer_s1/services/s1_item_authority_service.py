"""构装来源取证与内部资格回执；不在玩家 GET 路径触发 AI 或写入。"""

from copy import deepcopy

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1BuildingUsageLog, AdventurerS1Item,
)
from plugins.adventurer_s1.services.s1_item_authority_contract import (
    ITEM_AUTHORITY_REVIEW_KEY,
    authority_input_hash,
    build_item_authority_payload,
)
from plugins.adventurer_s1.services.s1_item_use_condition_service import (
    is_class_transfer_invitation,
    normalize_use_condition,
)


def assembly_log_query(realm_code: str):
    return AdventurerS1BuildingUsageLog.query.join(
        AdventurerS1, AdventurerS1.id == AdventurerS1BuildingUsageLog.adventurer_id,
    ).filter(
        AdventurerS1.realm_code == realm_code,
        AdventurerS1BuildingUsageLog.result['type'].as_string() == 'item_assembly_workshop',
        AdventurerS1BuildingUsageLog.result['success'].as_boolean().is_(True),
    )


def load_assembly_provenance(input_slots: list, *, realm_code: str,
                             source_index: dict | None = None) -> dict:
    """按精确成品名分层批查来源链，保留缺页；不从自由文本拆解 old_name。"""
    max_nodes = max(1, int(current_app.config.get(
        'ADVENTURER_S1_ITEM_AUTHORITY_MAX_SOURCE_NODES', 128,
    )))
    pending = {str(row.get('name') or '').strip() for row in input_slots}
    pending.discard('')
    seen_names, seen_logs = set(), set()
    origins, catalogs = [], []
    complete = True
    while pending:
        if len(seen_names) + len(pending) > max_nodes:
            complete = False
            break
        names = sorted(pending - seen_names)
        if not names:
            break
        seen_names.update(names)
        if source_index is None:
            rows = [catalog_authority_snapshot(row) for row in
                    AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).order_by(AdventurerS1Item.name).all()]
        else:
            rows = [source_index['catalogs'][name] for name in names if name in source_index['catalogs']]
        for row in rows:
            condition = normalize_use_condition(row['use_condition']) or {}
            review = condition.get(ITEM_AUTHORITY_REVIEW_KEY) or {}
            catalogs.append({
                'name': row['name'],
                'official': is_class_transfer_invitation(condition),
                'review_decision': review.get('decision') if isinstance(review, dict) else None,
            })
        remaining = max_nodes - len(origins)
        if source_index is None:
            logs = [{'id': log.id, 'result': deepcopy(log.result)} for log in
                    assembly_log_query(realm_code).filter(
                        AdventurerS1BuildingUsageLog.result['new_name'].as_string().in_(names),
                    ).order_by(AdventurerS1BuildingUsageLog.id).limit(remaining + 1).all()]
        else:
            logs = sorted([log for name in names for log in source_index['origins'].get(name, [])],
                          key=lambda log: log['id'])[:remaining + 1]
        if len(logs) > remaining:
            complete = False
            break
        pending = set()
        for log in logs:
            if log['id'] in seen_logs:
                continue
            seen_logs.add(log['id'])
            result = log['result'] or {}
            slots = result.get('input_slots')
            slots_available = isinstance(slots, list) and all(isinstance(row, dict) for row in slots)
            slots = slots if slots_available else []
            candidate = result.get('approved_candidate') or {}
            if not isinstance(candidate, dict):
                candidate = {}
            origins.append({
                'ref': f"origin:{log['id']}",
                'name': result.get('new_name'),
                'description': candidate.get('new_desc'),
                'inputs_available': slots_available,
                'input_slots': [{
                    'name': row.get('name'), 'type': row.get('type'),
                    'rarity': row.get('rarity'), 'description': row.get('desc'),
                } for row in slots],
            })
            complete = complete and slots_available
            pending.update(str(row.get('name') or '').strip() for row in slots)
        pending.discard('')
    return {'complete': complete and bool(input_slots), 'catalogs': catalogs, 'origins': origins}


def build_assembly_authority_input(*, input_slots: list, candidate: dict,
                                   realm_code: str) -> dict:
    return build_item_authority_payload(
        input_slots=input_slots, candidate=candidate,
        provenance=load_assembly_provenance(input_slots, realm_code=realm_code),
    )


def catalog_authority_snapshot(item) -> dict:
    return {
        'id': int(item.id), 'name': item.name, 'type': item.type,
        'rarity': item.rarity, 'description': item.description,
        'use_condition': deepcopy(item.use_condition),
    }


def authority_source_names(payload: dict) -> list[str]:
    names = {row['name'] for row in payload.get('input_slots', []) if row.get('name')}
    for origin in payload.get('provenance', {}).get('origins', []):
        if origin.get('name'):
            names.add(origin['name'])
        names.update(row['name'] for row in origin.get('input_slots', []) if row.get('name'))
    return sorted(names)


def apply_catalog_authority_review(item, *, review: dict, payload: dict,
                                   source: dict) -> dict:
    """调用方持有图鉴锁和短事务；只写内部条件，保留可还原的完整前值。"""
    from plugins.adventurer_s1.services.s1_item_authority_contract import normalize_item_authority_review

    normalized, error = normalize_item_authority_review(review, payload)
    candidate = payload.get('candidate') or {}
    if error or normalized is None:
        raise ValueError(error or 'item_authority_review_invalid')
    if candidate != {
        'name': item.name, 'type': item.type,
        'rarity': item.rarity, 'description': item.description,
    }:
        raise ValueError('item_authority_catalog_changed')
    before = deepcopy(item.use_condition)
    condition = dict(normalize_use_condition(before) or {})
    existing = condition.get(ITEM_AUTHORITY_REVIEW_KEY)
    if isinstance(existing, dict):
        if existing.get('input_hash') == normalized['input_hash']:
            return deepcopy(existing)
        # 已撤销资格不能因重新扫描或更换描述自动恢复；必须明确还原。
        if existing.get('decision') == 'unsupported':
            return deepcopy(existing)
    receipt = {**normalized, 'source': deepcopy(source),
               'previous_use_condition': before,
               'catalog_hash': authority_input_hash(candidate)}
    condition[ITEM_AUTHORITY_REVIEW_KEY] = receipt
    item.use_condition = condition
    return receipt


def apply_stored_authority_preview(preview: dict, *, realm_code: str, admin_user_id: int | None = None) -> str:
    """管理员预览的幂等写入；锁内按 realm、日志身份和完整图鉴前值重检。"""
    from plugins.adventurer_s1.services.s1_item_catalog_lock_service import lock_catalog_names
    from plugins.adventurer_s1.services.s1_realm_service import S1RealmService

    if S1RealmService.current_runtime_realm() != realm_code or preview.get('realm_code') != realm_code:
        raise ValueError('item_authority_realm_mismatch')
    before = preview['catalog_before']
    with db_manager.transaction():
        source_names = authority_source_names(preview['payload'])
        if not lock_catalog_names(source_names, shared=True, wait=False):
            return 'busy'
        if not lock_catalog_names([before['name']], shared=False, wait=False):
            return 'busy'
        item = AdventurerS1Item.query.filter_by(id=before['id']).with_for_update().first()
        if item is None:
            return 'missing'
        existing = (item.use_condition or {}).get(ITEM_AUTHORITY_REVIEW_KEY) or {}
        if existing.get('input_hash') == preview['payload']['input_hash']:
            return 'already_applied'
        if catalog_authority_snapshot(item) != before:
            return 'changed'
        log = assembly_log_query(realm_code).filter(
            AdventurerS1BuildingUsageLog.id == preview['source_log_id'],
        ).first()
        if log is None or authority_input_hash(log.result) != preview['source_log_hash']:
            return 'source_changed'
        slots = log.result.get('input_slots')
        if not isinstance(slots, list) or not all(isinstance(row, dict) for row in slots):
            slots = []
        expected_payload = build_assembly_authority_input(
            input_slots=slots,
            candidate={'new_name': item.name, 'new_type': item.type,
                       'new_desc': item.description, 'new_rarity': item.rarity},
            realm_code=realm_code,
        )
        if expected_payload != preview['payload']:
            return 'source_changed'
        apply_catalog_authority_review(
            item, review=preview['review'], payload=preview['payload'],
            source={'realm_code': realm_code, 'building_log_id': int(log.id), 'admin_user_id': admin_user_id},
        )
    return 'applied'


def restore_catalog_authority_review(preview: dict, *, realm_code: str) -> str:
    """按原预览和内部回执还原条件；后续编辑或另一裁决存在时拒绝覆盖。"""
    from plugins.adventurer_s1.services.s1_item_catalog_lock_service import lock_catalog_names
    from plugins.adventurer_s1.services.s1_realm_service import S1RealmService

    if S1RealmService.current_runtime_realm() != realm_code or preview.get('realm_code') != realm_code:
        raise ValueError('item_authority_realm_mismatch')
    before = preview['catalog_before']
    with db_manager.transaction():
        if not lock_catalog_names([before['name']], shared=False, wait=False):
            return 'busy'
        item = AdventurerS1Item.query.filter_by(id=before['id']).with_for_update().first()
        if item is None:
            return 'missing'
        if catalog_authority_snapshot(item) == before:
            return 'already_restored'
        current = catalog_authority_snapshot(item)
        condition = deepcopy(current['use_condition']) or {}
        receipt = condition.pop(ITEM_AUTHORITY_REVIEW_KEY, None)
        previous_without_review = deepcopy(before['use_condition']) or {}
        previous_without_review.pop(ITEM_AUTHORITY_REVIEW_KEY, None)
        if (not isinstance(receipt, dict)
                or receipt.get('input_hash') != preview['payload']['input_hash']
                or receipt.get('previous_use_condition') != before['use_condition']
                or condition != previous_without_review):
            return 'changed'
        current['use_condition'] = before['use_condition']
        if current != before:
            return 'changed'
        item.use_condition = deepcopy(before['use_condition'])
    return 'restored'


def admin_item_authority_action(body, *, action: str, admin_user_id: int):
    """认证管理员导入预览、执行或还原；不在 HTTP 请求里调用模型。"""
    from plugins.adventurer_s1.services.s1_item_authority_contract import normalize_item_authority_review
    from plugins.adventurer_s1.services.s1_realm_service import S1RealmService

    if not isinstance(body, dict) or not isinstance(body.get('preview'), dict):
        return None, '请提供完整资格判定预览'
    try:
        preview = {key: deepcopy(body['preview'][key]) for key in (
            'realm_code', 'source_log_id', 'source_log_hash', 'catalog_before', 'payload', 'review',
        )}
        before, payload = preview['catalog_before'], preview['payload']
        if type(before['id']) is not int or before['id'] <= 0 or type(preview['source_log_id']) is not int:
            return None, '预览身份无效'
        rebuilt = build_item_authority_payload(
            input_slots=payload['input_slots'], provenance=payload['provenance'],
            candidate={'new_name': before['name'], 'new_type': before['type'],
                       'new_rarity': before['rarity'], 'new_desc': before['description']},
        )
        if rebuilt != payload:
            return None, '预览内容或哈希发生变化'
        review, error = normalize_item_authority_review(preview['review'], payload)
        if error:
            return None, '资格判定合同无效'
        preview['review'] = review
        realm_code = S1RealmService.current_runtime_realm()
        if preview['realm_code'] != realm_code:
            return None, '预览与当前区服不符'
        if action == 'preview':
            item = AdventurerS1Item.query.filter_by(id=before['id']).first()
            log = assembly_log_query(realm_code).filter(
                AdventurerS1BuildingUsageLog.id == preview['source_log_id'],
            ).first()
            if item is None or log is None:
                return None, '未找到预览对应的物品或构装记录'
            if catalog_authority_snapshot(item) != before or authority_input_hash(log.result) != preview['source_log_hash']:
                return None, '预览对应的数据已发生变化，请重新检查'
            return {'status': 'ready', 'preview': preview}, None
        if action == 'execute':
            status = apply_stored_authority_preview(preview, realm_code=realm_code, admin_user_id=admin_user_id)
        elif action == 'restore':
            status = restore_catalog_authority_review(preview, realm_code=realm_code)
        else:
            return None, '操作无效'
        return {'status': status, 'catalog_id': before['id']}, None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, '资格判定预览结构无效'
