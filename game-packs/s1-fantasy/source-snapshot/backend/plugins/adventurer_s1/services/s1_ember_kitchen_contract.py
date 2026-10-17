"""余烬灶坊冻结输入与单次生成结果的机械合同。"""

import hashlib
import json
from typing import Optional

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


EMBER_KITCHEN_INPUT_CONTRACT_VERSION = 1
EMBER_KITCHEN_RARITY_TIERS = ('common', 'rare', 'epic', 'legendary')
EMBER_KITCHEN_RARITIES = frozenset(EMBER_KITCHEN_RARITY_TIERS)
EMBER_KITCHEN_COOKWARE = {
    'stew_pot': {
        'name': '厚底炖锅',
        'description': '以水与余温慢炖，适合汤羹、炖煮与需要充分熟化的食材。',
    },
    'frying_pan': {
        'name': '长柄煎锅',
        'description': '以油脂和高温煎炒，适合薄切、小块与需要焦香口感的食材。',
    },
    'steamer': {
        'name': '叠层蒸笼',
        'description': '以蒸汽加热，适合保留原味并避免食材直接接触炉火。',
    },
    'baking_tray': {
        'name': '黑铁烤盘',
        'description': '以炉膛干热烘烤，适合烤制、焙干与形成外壳。',
    },
    'stone_mortar': {
        'name': '石臼与研杵',
        'description': '用于捣碎、研磨与冷拌；不能替代必要的加热、去毒或熟化。',
    },
}
EMBER_KITCHEN_RETRY_REASON_DESCRIPTIONS = {
    'ember_kitchen_result_not_object': '只返回一个 JSON 对象作为完整烹饪结果',
    'ember_kitchen_result_is_edible_not_boolean': 'is_edible 必须是 JSON 布尔值 true 或 false',
    'ember_kitchen_result_narrative_invalid': 'narrative 必须是 1~500 字的非空玩家可见烹饪过程与结果',
    'ember_kitchen_result_new_name_invalid': '可食结果必须提供 1~20 字的非空 new_name',
    'ember_kitchen_result_new_desc_invalid': '可食结果必须提供 1~500 字、完整自包含的 new_desc',
    'ember_kitchen_result_uses_left_invalid': '可食结果的 uses_left 必须是 1 到本次食材总投入数量之间的整数',
}


def build_ember_kitchen_input_slots(item_snapshots: list[dict]) -> list[dict]:
    """把按实例冻结的食材快照展开成 1~5 个唯一权威槽位。"""
    slots: list[dict] = []
    for snapshot in item_snapshots:
        quantity = int(snapshot.get('input_qty') or 1)
        for _ in range(quantity):
            slots.append({
                'slot_id': len(slots) + 1,
                'source': str(snapshot.get('source') or ''),
                'id': int(snapshot.get('id')),
                'name': str(snapshot.get('name') or ''),
                'type': str(snapshot.get('type') or ''),
                'rarity': str(snapshot.get('rarity') or 'common'),
                'desc': str(snapshot.get('desc') or ''),
                'custom_desc': snapshot.get('custom_desc'),
                'uses_left': snapshot.get('uses_left'),
                'evolution_stage': max(0, int(snapshot.get('evolution_stage') or 0)),
                'damage_stage': snapshot.get('damage_stage'),
                'damage_note': snapshot.get('damage_note'),
            })
    return slots


def build_ember_kitchen_input_snapshot_hash(
    *,
    input_slots: list[dict],
    cookware_code: str,
    contract_version: int = EMBER_KITCHEN_INPUT_CONTRACT_VERSION,
) -> str:
    contract = {
        'contract_version': int(contract_version),
        'input_slots': input_slots,
        'cookware_code': str(cookware_code),
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def build_ember_kitchen_request_payload(
    *,
    input_slots: list[dict],
    cookware_code: str,
) -> dict:
    """生成掌勺 AI 的最小冻结请求投影，不暴露实例 id。"""
    cookware = EMBER_KITCHEN_COOKWARE[cookware_code]
    return {
        'cookware': {
            'code': cookware_code,
            'name': cookware['name'],
            'description': cookware['description'],
        },
        'ingredient_count': len(input_slots),
        'ingredients': [{
            'slot_id': item.get('slot_id'),
            'name': item.get('name'),
            'type': item.get('type'),
            'rarity': item.get('rarity'),
            'description': item.get('desc') or '无描述',
            'uses_left': item.get('uses_left'),
            'evolution_stage': item.get('evolution_stage', 0),
            'damage_stage': item.get('damage_stage'),
            'damage_note': item.get('damage_note'),
        } for item in input_slots],
    }


def validate_ember_kitchen_item_snapshots(
    raw_snapshots: object,
    *,
    expected_refs: list[dict],
) -> tuple[Optional[list[dict]], Optional[str]]:
    """校验内部删除前回执与紧凑稳定引用一一对应，缺证时禁止消费。"""
    if not isinstance(raw_snapshots, list) or len(raw_snapshots) != len(expected_refs):
        return None, 'ember_kitchen_item_snapshots_count_mismatch'

    normalized: list[dict] = []
    for raw, expected_ref in zip(raw_snapshots, expected_refs):
        if not isinstance(raw, dict):
            return None, 'ember_kitchen_item_snapshot_not_object'
        try:
            item_id = int(raw.get('id'))
            input_qty = int(raw.get('input_qty'))
            quantity_at_start = int(raw.get('quantity_at_start'))
            evolution_stage = int(raw.get('evolution_stage'))
            recovery_minutes = int(raw.get('recovery_minutes'))
            expected_id = int(expected_ref.get('id'))
            expected_qty = int(expected_ref.get('quantity') or 1)
        except (TypeError, ValueError):
            return None, 'ember_kitchen_item_snapshot_numeric_invalid'
        source = str(raw.get('source') or '').strip()
        if (
            source != str(expected_ref.get('source') or '')
            or item_id != expected_id
            or input_qty != expected_qty
            or input_qty < 1
            or quantity_at_start < input_qty
            or evolution_stage < 0
            or recovery_minutes < 0
        ):
            return None, 'ember_kitchen_item_snapshot_ref_mismatch'
        if (
            not isinstance(raw.get('name'), str)
            or not str(raw.get('name') or '').strip()
            or not isinstance(raw.get('type'), str)
            or not str(raw.get('type') or '').strip()
            or raw.get('rarity') not in EMBER_KITCHEN_RARITIES
            or not isinstance(raw.get('desc'), str)
            or type(raw.get('is_player_locked')) is not bool
        ):
            return None, 'ember_kitchen_item_snapshot_identity_invalid'
        for nullable_text_key in (
            'custom_desc', 'recovery_started_at', 'recovery_ends_at',
            'damage_stage', 'damage_note',
        ):
            value = raw.get(nullable_text_key)
            if value is not None and not isinstance(value, str):
                return None, 'ember_kitchen_item_snapshot_text_invalid'
        uses_left = raw.get('uses_left')
        if uses_left is not None and (
            isinstance(uses_left, bool) or not isinstance(uses_left, int)
        ):
            return None, 'ember_kitchen_item_snapshot_uses_left_invalid'
        if not isinstance(raw.get('created_at'), str) or not raw['created_at']:
            return None, 'ember_kitchen_item_snapshot_timestamp_invalid'
        if not isinstance(raw.get('updated_at'), str) or not raw['updated_at']:
            return None, 'ember_kitchen_item_snapshot_timestamp_invalid'
        normalized.append(dict(raw))
    return normalized, None


def validate_ember_kitchen_input_slots(
    raw_slots: object,
    *,
    expected_refs: list[dict],
) -> tuple[Optional[list[dict]], Optional[str]]:
    """逐层投影冻结食材，并校验稳定引用与待消耗引用完全一致。"""
    if not isinstance(raw_slots, list) or len(raw_slots) != len(expected_refs):
        return None, 'ember_kitchen_input_slots_count_mismatch'

    normalized: list[dict] = []
    for expected_slot_id, (raw_slot, expected_ref) in enumerate(
        zip(raw_slots, expected_refs), 1,
    ):
        if not isinstance(raw_slot, dict):
            return None, 'ember_kitchen_input_slot_not_object'
        try:
            slot_id = int(raw_slot.get('slot_id'))
            item_id = int(raw_slot.get('id'))
        except (TypeError, ValueError):
            return None, 'ember_kitchen_input_slot_identity_invalid'
        source = str(raw_slot.get('source') or '').strip()
        if (
            slot_id != expected_slot_id
            or source != str(expected_ref.get('source') or '')
            or item_id != int(expected_ref.get('id'))
        ):
            return None, 'ember_kitchen_input_slot_ref_mismatch'

        name = str(raw_slot.get('name') or '').strip()
        item_type = str(raw_slot.get('type') or '').strip()
        rarity = str(raw_slot.get('rarity') or '').strip()
        desc = raw_slot.get('desc')
        custom_desc = raw_slot.get('custom_desc')
        uses_left = raw_slot.get('uses_left')
        evolution_stage = raw_slot.get('evolution_stage', 0)
        damage_stage = raw_slot.get('damage_stage')
        damage_note = raw_slot.get('damage_note')
        if not name or not item_type:
            return None, 'ember_kitchen_input_slot_text_invalid'
        if rarity not in EMBER_KITCHEN_RARITIES or not isinstance(desc, str):
            return None, 'ember_kitchen_input_slot_item_invalid'
        if uses_left is not None and (
            isinstance(uses_left, bool) or not isinstance(uses_left, int)
        ):
            return None, 'ember_kitchen_input_slot_uses_left_invalid'
        if (
            isinstance(evolution_stage, bool)
            or not isinstance(evolution_stage, int)
            or evolution_stage < 0
        ):
            return None, 'ember_kitchen_input_slot_evolution_stage_invalid'
        if custom_desc is not None and not isinstance(custom_desc, str):
            return None, 'ember_kitchen_input_slot_custom_desc_invalid'
        if damage_stage is not None and not isinstance(damage_stage, str):
            return None, 'ember_kitchen_input_slot_damage_stage_invalid'
        if damage_note is not None and not isinstance(damage_note, str):
            return None, 'ember_kitchen_input_slot_damage_note_invalid'

        normalized.append({
            'slot_id': slot_id,
            'source': source,
            'id': item_id,
            'name': name,
            'type': item_type,
            'rarity': rarity,
            'desc': desc,
            'custom_desc': custom_desc,
            'uses_left': uses_left,
            'evolution_stage': evolution_stage,
            'damage_stage': damage_stage,
            'damage_note': damage_note,
            '_custom_desc_frozen': 'custom_desc' in raw_slot,
            '_damage_stage_frozen': 'damage_stage' in raw_slot,
            '_damage_note_frozen': 'damage_note' in raw_slot,
        })
    return normalized, None


def normalize_ember_kitchen_ai_result(
    raw_result: object,
    *,
    max_uses: int,
) -> tuple[Optional[dict], Optional[str]]:
    """白名单投影烹饪结果；不可食分支不消费任何成品 sibling。"""
    if not isinstance(raw_result, dict):
        return None, 'ember_kitchen_result_not_object'

    is_edible, _ = normalize_exact_boolean(raw_result.get('is_edible'))
    narrative = raw_result.get('narrative')
    if not isinstance(is_edible, bool):
        return None, 'ember_kitchen_result_is_edible_not_boolean'
    if (
        not isinstance(narrative, str)
        or not narrative.strip()
        or len(narrative.strip()) > 500
    ):
        return None, 'ember_kitchen_result_narrative_invalid'

    if not is_edible:
        return {
            'is_edible': False,
            'new_name': '',
            'new_desc': '',
            'uses_left': 0,
            'narrative': narrative.strip(),
        }, None

    new_name = raw_result.get('new_name')
    new_desc = raw_result.get('new_desc')
    uses_left, _ = normalize_ascii_integer(raw_result.get('uses_left'))
    if not isinstance(new_name, str) or not (1 <= len(new_name.strip()) <= 20):
        return None, 'ember_kitchen_result_new_name_invalid'
    if (
        not isinstance(new_desc, str)
        or not (1 <= len(new_desc.strip()) <= 500)
    ):
        return None, 'ember_kitchen_result_new_desc_invalid'
    if (
        isinstance(uses_left, bool)
        or not isinstance(uses_left, int)
        or uses_left < 1
        or uses_left > max_uses
    ):
        return None, 'ember_kitchen_result_uses_left_invalid'
    return {
        'is_edible': True,
        'new_name': new_name.strip(),
        'new_desc': new_desc.strip(),
        'uses_left': uses_left,
        'narrative': narrative.strip(),
    }, None
