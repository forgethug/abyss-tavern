"""Expand exact Observer item edits against its frozen settlement candidate."""
from copy import deepcopy
import json

from .s1_ai_output_normalization import normalize_ascii_integer
from .s1_settlement_field_projection import project_s1_member_settlement_fields

ITEM_DELTA_MODE = 'item_delta_v1'
ITEM_FIELDS = frozenset({'new_equipments', 'consumed_equipments', 'equipment_updates'})

ITEM_DELTA_RULES = """【物品审核的局部交付】
本轮 item_patch_mode=item_delta_v1：原结算作为基线，未涉及的成员与条目由后端原样保留。物品的新增、消耗、修改只通过顶层 item_changes 数组声明实际纠错，不重抄整份物品集合；其余变化仍使用 override_fields。
每项包含 member_id（本队成员整数ID）、field（new_equipments / consumed_equipments / equipment_updates）、key、op（set / remove）。new_equipments 的 key 精确复制自然物品名；consumed_equipments 的 key 精确复制 name；equipment_updates 的 key 是出发装备 equipment_id 整数。相同成员、字段和 key 只交付一次。
set 另含 value 对象：新增物品使用原物品字段合同，消耗使用原消耗条目合同，装备修改使用原修改条目合同；新物品名称由 key 承担，其余两类 value 的身份字段与 key 一致。它只添加或替换该条目。remove 只删除候选结算里确实存在的该条目，不删除出发库存；未列入候选的物品不需要删除。未知或不匹配的目标不能执行。
从正文双向核对：未实际发生的物品取得、转交、消耗或持久修改，移除对应错误候选；普通使用本身不产生消耗或修改。正文已明确实际取得、最终个人归属和带出而漏账的物品，补到对应成员，保持同批数量守恒。合法现有项保持不变，不补造未发生的取得、使用或交易。
没有物品纠错时省略 item_changes。正文存在不能靠结算修正的关键因果硬伤，仍使用既有重生成出口；不为补字段重写已经成立的事实。
"""


def _whole_item_entries(value, field):
    """Index a complete representation without guessing or collapsing identities."""
    if field == 'new_equipments':
        if not isinstance(value, dict):
            return None, 'item_changes_source_not_object'
        entries = value
    else:
        if not isinstance(value, list):
            return None, 'item_changes_source_not_array'
        entries = {}
        identity = 'equipment_id' if field == 'equipment_updates' else 'name'
        for item in value:
            if not isinstance(item, dict):
                return None, 'item_changes_source_entry_invalid'
            key = item.get(identity)
            if (field == 'equipment_updates' and (type(key) is not int or key <= 0)
                    or field != 'equipment_updates' and (not isinstance(key, str) or not key.strip() or key != key.strip())):
                return None, 'item_changes_source_identity_invalid'
            if key in entries:
                return None, 'item_changes_source_identity_ambiguous'
            entries[key] = item
    if any(not isinstance(k, (str, int)) or not isinstance(v, dict) for k, v in entries.items()):
        return None, 'item_changes_source_entry_invalid'
    return entries, None


def expand_observer_item_changes(result: dict, source: dict | None, *, required: bool):
    """Return canonical complete fields, or a strict identity/shape error.

    Compact edits are retained internally for deterministic revalidation against
    the same frozen candidate. They never directly reach an inventory consumer.
    """
    overrides = result.get('override_fields', {})
    if overrides is None and 'override_fields' in result:
        overrides = {}
    if not isinstance(overrides, dict):
        return result, None  # The ordinary validator owns this shape error.
    # Lift this unique field before the ordinary override whitelist discards it.
    # Conflicting copies cannot establish an unambiguous intended correction.
    if 'item_changes' in overrides:
        nested = overrides['item_changes']
        if ('item_changes' in result
                and json.dumps(result['item_changes'], sort_keys=True) != json.dumps(nested, sort_keys=True)):
            return None, 'item_changes_conflicting_locations'
        result = deepcopy(result)
        result['item_changes'] = deepcopy(nested)
        overrides = {key: value for key, value in overrides.items() if key != 'item_changes'}
    raw_changes = result.get('item_changes', [])
    if not isinstance(raw_changes, list):
        return None, 'item_changes_not_array'
    if not required and 'item_changes' not in result:
        return result, None
    out = deepcopy(result)
    out['override_fields'] = deepcopy(overrides)
    raw_rows = overrides.get('member_updates', [])
    if not isinstance(raw_rows, list):
        return None, 'item_changes_member_updates_not_array'
    rows = deepcopy(raw_rows)
    patches = {}
    for row in rows:
        if not isinstance(row, dict):
            return None, 'item_changes_member_patch_not_object'
        mid, _ = normalize_ascii_integer(row.get('id'), minimum=1)
        if type(mid) is not int or mid in patches:
            return None, 'item_changes_member_identity_invalid'
        patches[mid] = row
    members = {}
    for row in (source or {}).get('member_updates', []):
        if isinstance(row, dict) and type(row.get('id')) is int:
            if row['id'] in members:
                return None, 'item_changes_ambiguous_source_member'
            members[row['id']] = row
    # Legacy complete collections can express unambiguous entry replacements.
    # Infer only set operations: omission is not permission to erase a sibling.
    explicit_pairs = {(normalize_ascii_integer(e.get('member_id'), minimum=1)[0], e.get('field')) for e in raw_changes
                      if isinstance(e, dict) and isinstance(e.get('member_id'), (str, int))
                      and isinstance(e.get('field'), str)}
    raw_changes = deepcopy(raw_changes)
    inferred_pairs = set()
    if required:
        for mid, row in patches.items():
            for field in sorted(ITEM_FIELDS.intersection(row)):
                if (mid, field) in explicit_pairs:
                    continue  # The existing comparison checks both representations.
                if mid not in members:
                    return None, 'item_changes_unknown_member'
                baseline = members[mid].get(field, {} if field == 'new_equipments' else [])
                before, before_error = _whole_item_entries(baseline, field)
                after, after_error = _whole_item_entries(row[field], field)
                if before_error or after_error:
                    return None, before_error or after_error
                if not before.keys() <= after.keys():
                    return None, 'item_changes_whole_field_override_forbidden'
                for key, value in after.items():
                    if key not in before or before[key] != value:
                        raw_changes.append({'member_id': mid, 'field': field,
                                            'key': key, 'op': 'set', 'value': deepcopy(value)})
                        inferred_pairs.add((mid, field))
    changed = {}
    seen = set()
    normalized_changes = []
    for raw in raw_changes:
        if not isinstance(raw, dict):
            return None, 'item_changes_entry_not_object'
        edit = {k: deepcopy(raw[k]) for k in ('member_id', 'field', 'key', 'op', 'value') if k in raw}
        mid, _ = normalize_ascii_integer(edit.get('member_id'), minimum=1)
        field, key, op = edit.get('field'), edit.get('key'), edit.get('op')
        if type(mid) is not int or mid not in members:
            return None, 'item_changes_unknown_member'
        if not isinstance(field, str) or field not in ITEM_FIELDS or op not in ('set', 'remove'):
            return None, 'item_changes_field_or_operation_invalid'
        identity_field = 'equipment_id' if field == 'equipment_updates' else 'name'
        if field == 'equipment_updates':
            key, _ = normalize_ascii_integer(key, minimum=1)
            if type(key) is not int:
                return None, 'item_changes_equipment_identity_invalid'
        elif not isinstance(key, str) or not key.strip() or key != key.strip():
            return None, 'item_changes_name_invalid'
        identity = (mid, field, key)
        if identity in seen:
            return None, 'item_changes_duplicate_target'
        seen.add(identity)
        pair = (mid, field)
        if pair not in changed:
            baseline = members[mid].get(field, {} if field == 'new_equipments' else [])
            if field == 'new_equipments':
                if not isinstance(baseline, dict):
                    return None, 'item_changes_source_not_object'
                changed[pair] = deepcopy(baseline)
            else:
                if not isinstance(baseline, list):
                    return None, 'item_changes_source_not_array'
                entries = {}
                for entry in baseline:
                    if not isinstance(entry, dict):
                        return None, 'item_changes_source_entry_invalid'
                    entry_key = entry.get(identity_field)
                    if (field == 'equipment_updates' and type(entry_key) is not int
                            or field != 'equipment_updates' and not isinstance(entry_key, str)):
                        return None, 'item_changes_source_identity_invalid'
                    if entry_key in entries:
                        return None, 'item_changes_source_identity_ambiguous'
                    entries[entry_key] = deepcopy(entry)
                changed[pair] = entries
        entries = changed[pair]
        if op == 'remove':
            if key not in entries:
                return None, 'item_changes_remove_target_missing'
            entries.pop(key)
            edit.pop('value', None)
        else:
            value = edit.get('value')
            if not isinstance(value, dict):
                return None, 'item_changes_value_not_object'
            if field == 'equipment_updates':
                value_id, _ = normalize_ascii_integer(value.get(identity_field), minimum=1)
                if type(value_id) is not int or value_id != key:
                    return None, 'item_changes_value_identity_mismatch'
                value[identity_field] = value_id
            if field != 'new_equipments' and value.get(identity_field) != key:
                return None, 'item_changes_value_identity_mismatch'
            if field == 'new_equipments':
                value = project_s1_member_settlement_fields({field: {key: value}})[field][key]
            elif field == 'consumed_equipments':
                value = {name: deepcopy(value[name]) for name in ('name', 'quantity') if name in value}
            edit['value'] = value
            entries[key] = deepcopy(value)
        edit.update(member_id=mid, key=key)
        normalized_changes.append(edit)
    # A copied unchanged field is harmless. A changed whole collection cannot
    # bypass the new contract, including via a second full-field representation.
    for mid, row in patches.items():
        for field in ITEM_FIELDS.intersection(row):
            pair = (mid, field)
            expected = changed.get(pair, members.get(mid, {}).get(field, {} if field == 'new_equipments' else []))
            if pair in changed and field != 'new_equipments':
                expected = list(expected.values())
            if pair in inferred_pairs:
                row[field] = deepcopy(expected)
            if required and row[field] != expected:
                return None, 'item_changes_whole_field_override_forbidden'
            if pair in changed and row[field] != expected:
                return None, 'item_changes_conflicting_override'
            if required and pair not in changed:
                row.pop(field)
    for (mid, field), entries in changed.items():
        if mid not in patches:
            patches[mid] = {'id': mid}
            rows.append(patches[mid])
        patches[mid][field] = entries if field == 'new_equipments' else list(entries.values())
    rows = [row for row in rows if any(k not in ('id', 'name') for k in row)]
    if rows:
        out['override_fields']['member_updates'] = rows
    else:
        out['override_fields'].pop('member_updates', None)
    if 'item_changes' in result or inferred_pairs:
        out['item_changes'] = normalized_changes
    return out, None


def capture_observer_item_changes(overrides: dict, source: dict | None) -> list[dict]:
    """Freeze validated canonical edits so a second validation is lossless.

    Ordinary validators may project aliases/defaults or apply continuity receipts.
    Capture their complete result, never any ignored raw AI fields.
    """
    members = {row['id']: row for row in (source or {}).get('member_updates', [])
               if isinstance(row, dict) and type(row.get('id')) is int}
    changes = []
    for row in overrides.get('member_updates', []):
        mid = row['id']
        baseline = members.get(mid, {})
        for field in sorted(ITEM_FIELDS.intersection(row)):
            identity = 'equipment_id' if field == 'equipment_updates' else 'name'
            before = baseline.get(field, {} if field == 'new_equipments' else [])
            after = row[field]
            if field != 'new_equipments':
                before = {item[identity]: item for item in before}
                after = {item[identity]: item for item in after}
            for key in before:
                if key not in after:
                    changes.append({'member_id': mid, 'field': field, 'key': key, 'op': 'remove'})
            for key, value in after.items():
                if key not in before or before[key] != value:
                    changes.append({'member_id': mid, 'field': field, 'key': key,
                                    'op': 'set', 'value': deepcopy(value)})
    return changes
