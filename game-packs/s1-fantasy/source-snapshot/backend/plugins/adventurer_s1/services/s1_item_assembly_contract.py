"""构装工坊输入快照与 AI 输出的机械合同。"""

import hashlib
import json
from typing import Optional

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


ITEM_ASSEMBLY_LEGACY_INPUT_CONTRACT_VERSION = 1
ITEM_ASSEMBLY_INPUT_CONTRACT_VERSION = 2
ITEM_ASSEMBLY_OUTPUT_TYPES = frozenset({
    'weapon', 'armor', 'shield', 'accessory',
    'consumable', 'potion', 'scroll',
    'material', 'tool', 'document', 'key_item', 'special',
})
ITEM_ASSEMBLY_RARITY_TIERS = ('common', 'rare', 'epic', 'legendary')
ITEM_ASSEMBLY_RARITIES = frozenset(ITEM_ASSEMBLY_RARITY_TIERS)
ITEM_ASSEMBLY_DIRECTION_FITS = frozenset({
    'not_requested', 'accepted', 'adjusted',
})
ITEM_ASSEMBLY_DIRECTION_REASON_MAX_LENGTH = 300
ITEM_ASSEMBLY_GENERATION_RETRY_REASON_CODES = frozenset({
    'assembly_result_not_object',
    'assembly_result_success_not_boolean',
    'assembly_result_narrative_invalid',
    'assembly_result_new_name_invalid',
    'assembly_result_new_name_conflict',
    'assembly_result_new_type_invalid',
    'assembly_result_new_desc_invalid',
    'assembly_result_new_rarity_invalid',
    'assembly_result_direction_fit_invalid',
    'assembly_result_direction_reason_invalid',
    'assembly_result_direction_request_mismatch',
    'assembly_result_consumed_slots_not_array',
    'assembly_result_unreasonable_not_boolean',
    'assembly_result_consumed_slots_overflow',
    'assembly_result_consumed_slot_not_integer',
    'assembly_result_consumed_slot_out_of_range',
    'assembly_result_consumed_slot_duplicate',
    'assembly_result_unreasonable_requires_all_slots',
    'assembly_result_ordinary_failure_cannot_consume_all_slots',
})
ITEM_ASSEMBLY_GENERATION_RETRY_REASON_DESCRIPTIONS = {
    'assembly_result_not_object': '只返回一个 JSON 对象作为完整构装结果',
    'assembly_result_success_not_boolean': 'success 必须是 JSON 布尔值 true 或 false',
    'assembly_result_narrative_invalid': 'narrative 必须是 1~500 字的非空玩家可见过程与结果',
    'assembly_result_new_name_invalid': '成功结果必须提供 1~20 字的非空 new_name',
    'assembly_result_new_name_conflict': '成功产物必须改用一个尚未被既有图鉴占用的新名称',
    'assembly_result_new_type_invalid': '成功结果的 new_type 必须只取当前合同允许的一个物品类型',
    'assembly_result_new_desc_invalid': '成功结果必须提供非空、完整自包含的 new_desc',
    'assembly_result_new_rarity_invalid': '成功结果的 new_rarity 必须只取 common、rare、epic 或 legendary',
    'assembly_result_direction_fit_invalid': '有申请方向的成功结果必须提供 direction_fit，且只能为 accepted 或 adjusted',
    'assembly_result_direction_reason_invalid': '只有 direction_fit=adjusted 时 reason 才能且必须提供 1~300 字的玩家可见构装师说明',
    'assembly_result_direction_request_mismatch': '有申请方向时 direction_fit 只能为 accepted 或 adjusted；not_requested 由后端在无申请时派生',
    'assembly_result_consumed_slots_not_array': '失败结果必须提供 JSON 数组 consumed_slots，可为空数组',
    'assembly_result_unreasonable_not_boolean': '失败结果的 unreasonable_input 必须是 JSON 布尔值',
    'assembly_result_consumed_slots_overflow': 'consumed_slots 的成员数不能超过本次冻结槽位总数',
    'assembly_result_consumed_slot_not_integer': 'consumed_slots 的每个成员必须是整数槽位编号',
    'assembly_result_consumed_slot_out_of_range': 'consumed_slots 只能引用本次 1~N 的冻结槽位编号',
    'assembly_result_consumed_slot_duplicate': 'consumed_slots 必须按槽位去重且升序排列',
    'assembly_result_unreasonable_requires_all_slots': 'unreasonable_input=true 时 consumed_slots 必须准确包含全部 1~N 槽位',
    'assembly_result_ordinary_failure_cannot_consume_all_slots': '普通失败最多损毁 N-1 个槽位；全损必须改走满足逐槽因果的专属出口',
}
ITEM_ASSEMBLY_RETRY_REASON_CODES = ITEM_ASSEMBLY_GENERATION_RETRY_REASON_CODES
ITEM_ASSEMBLY_RETRY_REASON_DESCRIPTIONS = (
    ITEM_ASSEMBLY_GENERATION_RETRY_REASON_DESCRIPTIONS
)
ITEM_ASSEMBLY_LAYOUT_LABELS = {
    'vertical_line': '从上到下一条直线',
    'cross': '上下左右围绕中心',
    'inside_out': '从里到外分层包覆',
    'left_to_right': '从左到右横向串联',
    'ring': '环形闭环',
}


def assembly_layout_position(layout_mode: str, index: int, total: int) -> str:
    if layout_mode == 'vertical_line':
        return ['顶部', '上段', '中段', '下段', '底部'][index - 1] if total == 5 else f'第 {index} 段'
    if layout_mode == 'cross':
        labels = ['中心', '上方', '右方', '下方', '左方']
        return labels[index - 1] if index <= len(labels) else f'槽位 {index}'
    if layout_mode == 'inside_out':
        labels = ['核心', '内层一', '内层二', '外层一', '外层二']
        return labels[index - 1] if index <= len(labels) else f'槽位 {index}'
    if layout_mode == 'left_to_right':
        return ['最左', '左段', '中段', '右段', '最右'][index - 1] if total == 5 else f'第 {index} 段'
    if layout_mode == 'ring':
        labels = ['起点', '右上', '右下', '左下', '左上']
        return labels[index - 1] if index <= len(labels) else f'环位 {index}'
    return f'槽位 {index}'


def build_assembly_input_slots(
    item_snapshots: list[dict],
    *,
    layout_mode: str,
) -> list[dict]:
    """把按实例冻结的快照展开成按槽位冻结的唯一权威输入。"""
    total = sum(int(item.get('input_qty') or 1) for item in item_snapshots)
    slots: list[dict] = []
    for snapshot in item_snapshots:
        quantity = int(snapshot.get('input_qty') or 1)
        for _ in range(quantity):
            slot_id = len(slots) + 1
            slots.append({
                'slot_id': slot_id,
                'position': assembly_layout_position(layout_mode, slot_id, total),
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


def build_assembly_input_snapshot_hash(
    *,
    input_slots: list[dict],
    layout_mode: str,
    desired_direction: str,
    contract_version: int = ITEM_ASSEMBLY_INPUT_CONTRACT_VERSION,
) -> str:
    contract = {
        'contract_version': int(contract_version),
        'input_slots': input_slots,
        'layout_mode': str(layout_mode),
        'desired_direction': str(desired_direction),
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def build_item_assembly_request_payload(
    *,
    input_slots: list[dict],
    layout_mode: str,
    desired_direction: str,
) -> dict:
    """构造构装师生成所需的最小冻结请求投影。"""
    prompt_slots = [{
        'slot_id': item.get('slot_id'),
        'position': item.get('position'),
        'source': '随身' if item.get('source') == 'carry' else '仓库',
        'name': item.get('name'),
        'type': item.get('type'),
        'rarity': item.get('rarity'),
        'description': item.get('desc') or '无描述',
        'uses_left': item.get('uses_left'),
        'evolution_stage': item.get('evolution_stage', 0),
        'damage_stage': item.get('damage_stage'),
        'damage_note': item.get('damage_note'),
    } for item in input_slots]
    return {
        'layout_mode': layout_mode,
        'layout_label': ITEM_ASSEMBLY_LAYOUT_LABELS[layout_mode],
        'input_slots': prompt_slots,
        'desired_direction': desired_direction or None,
    }


def build_legacy_assembly_input_snapshot_hash(
    *,
    item_snapshots: list[dict],
    layout_mode: str,
    layout_slots: list[dict],
    desired_direction: str,
) -> str:
    """仅用于校验升级前已经开始、尚未完成的 v1 会话。"""
    contract = {
        'items': item_snapshots,
        'layout_mode': str(layout_mode),
        'layout_slots': layout_slots,
        'desired_direction': str(desired_direction),
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_assembly_input_slots(
    raw_slots: object,
    *,
    expected_refs: list[dict],
) -> tuple[Optional[list[dict]], Optional[str]]:
    """逐层投影并验证冻结槽位与待消耗稳定引用完全一致。"""
    if not isinstance(raw_slots, list) or len(raw_slots) != len(expected_refs):
        return None, 'assembly_input_slots_count_mismatch'

    normalized: list[dict] = []
    for expected_slot_id, (raw_slot, expected_ref) in enumerate(zip(raw_slots, expected_refs), 1):
        if not isinstance(raw_slot, dict):
            return None, 'assembly_input_slot_not_object'
        try:
            slot_id = int(raw_slot.get('slot_id'))
            item_id = int(raw_slot.get('id'))
        except (TypeError, ValueError):
            return None, 'assembly_input_slot_identity_invalid'
        source = str(raw_slot.get('source') or '').strip()
        if (
            slot_id != expected_slot_id
            or source != str(expected_ref.get('source') or '')
            or item_id != int(expected_ref.get('id'))
        ):
            return None, 'assembly_input_slot_ref_mismatch'

        position = str(raw_slot.get('position') or '').strip()
        name = str(raw_slot.get('name') or '').strip()
        item_type = str(raw_slot.get('type') or '').strip()
        rarity = str(raw_slot.get('rarity') or '').strip()
        desc = raw_slot.get('desc')
        custom_desc = raw_slot.get('custom_desc')
        uses_left = raw_slot.get('uses_left')
        evolution_stage = raw_slot.get('evolution_stage', 0)
        damage_stage = raw_slot.get('damage_stage')
        damage_note = raw_slot.get('damage_note')
        if not position or len(position) > 20 or not name or not item_type:
            return None, 'assembly_input_slot_text_invalid'
        if rarity not in ITEM_ASSEMBLY_RARITIES or not isinstance(desc, str):
            return None, 'assembly_input_slot_item_invalid'
        if uses_left is not None and (isinstance(uses_left, bool) or not isinstance(uses_left, int)):
            return None, 'assembly_input_slot_uses_left_invalid'
        if isinstance(evolution_stage, bool) or not isinstance(evolution_stage, int) or evolution_stage < 0:
            return None, 'assembly_input_slot_evolution_stage_invalid'
        if custom_desc is not None and not isinstance(custom_desc, str):
            return None, 'assembly_input_slot_custom_desc_invalid'
        if damage_stage is not None and not isinstance(damage_stage, str):
            return None, 'assembly_input_slot_damage_stage_invalid'
        if damage_note is not None and not isinstance(damage_note, str):
            return None, 'assembly_input_slot_damage_note_invalid'

        normalized.append({
            'slot_id': slot_id,
            'position': position,
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


def normalize_item_assembly_ai_result(
    raw_result: object,
    *,
    slot_count: int,
    input_rarities: Optional[list[str]] = None,
    desired_direction: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """先白名单投影，再严格校验构装 AI 当前输出合同。"""
    if not isinstance(raw_result, dict):
        return None, 'assembly_result_not_object'

    success, _ = normalize_exact_boolean(raw_result.get('success'))
    narrative = raw_result.get('narrative')
    if not isinstance(success, bool):
        return None, 'assembly_result_success_not_boolean'
    if not isinstance(narrative, str) or not narrative.strip() or len(narrative.strip()) > 500:
        return None, 'assembly_result_narrative_invalid'

    if success:
        new_name = raw_result.get('new_name')
        new_type = raw_result.get('new_type')
        new_desc = raw_result.get('new_desc')
        new_rarity = raw_result.get('new_rarity')
        direction_fit = raw_result.get('direction_fit')
        direction_reason = raw_result.get('reason')
        if not isinstance(new_name, str) or not (1 <= len(new_name.strip()) <= 20):
            return None, 'assembly_result_new_name_invalid'
        if not isinstance(new_type, str) or new_type not in ITEM_ASSEMBLY_OUTPUT_TYPES:
            return None, 'assembly_result_new_type_invalid'
        if not isinstance(new_desc, str) or not new_desc.strip():
            return None, 'assembly_result_new_desc_invalid'
        if not isinstance(new_rarity, str) or new_rarity not in ITEM_ASSEMBLY_RARITIES:
            return None, 'assembly_result_new_rarity_invalid'
        direction_requested = bool(str(desired_direction or '').strip())
        if direction_requested:
            if (
                not isinstance(direction_fit, str)
                or direction_fit not in ITEM_ASSEMBLY_DIRECTION_FITS
            ):
                return None, 'assembly_result_direction_fit_invalid'
            if direction_fit == 'not_requested':
                return None, 'assembly_result_direction_request_mismatch'
            if not isinstance(direction_reason, str):
                return None, 'assembly_result_direction_reason_invalid'
            direction_reason = direction_reason.strip()
            if direction_fit == 'adjusted':
                if (
                    not direction_reason
                    or len(direction_reason) > ITEM_ASSEMBLY_DIRECTION_REASON_MAX_LENGTH
                ):
                    return None, 'assembly_result_direction_reason_invalid'
            elif direction_reason:
                return None, 'assembly_result_direction_reason_invalid'
        else:
            # 方向范围未激活时不消费 AI 偶发提交的 sibling；后端唯一派生无申请回执。
            direction_fit = 'not_requested'
            direction_reason = ''
        if input_rarities:
            input_indices = [
                ITEM_ASSEMBLY_RARITY_TIERS.index(rarity)
                for rarity in input_rarities
                if rarity in ITEM_ASSEMBLY_RARITIES
            ]
            base_index = max(input_indices, default=0)
            proposed_index = ITEM_ASSEMBLY_RARITY_TIERS.index(new_rarity)
            new_rarity = ITEM_ASSEMBLY_RARITY_TIERS[
                max(base_index, min(proposed_index, base_index + 1))
            ]
        return {
            'success': True,
            'new_name': new_name.strip(),
            'new_type': new_type,
            'new_desc': new_desc.strip(),
            'new_rarity': new_rarity,
            'direction_fit': direction_fit,
            'reason': direction_reason,
            'narrative': narrative.strip(),
            # 成功固定消费全部槽位；这里补成显式权威结果供结算与审计读取。
            'consumed_slots': list(range(1, slot_count + 1)),
            'unreasonable_input': False,
        }, None

    raw_slots = raw_result.get('consumed_slots')
    unreasonable_input, _ = normalize_exact_boolean(
        raw_result.get('unreasonable_input'),
    )
    if not isinstance(raw_slots, list):
        return None, 'assembly_result_consumed_slots_not_array'
    if not isinstance(unreasonable_input, bool):
        return None, 'assembly_result_unreasonable_not_boolean'
    if len(raw_slots) > slot_count:
        return None, 'assembly_result_consumed_slots_overflow'

    consumed_slots: list[int] = []
    seen_slots: set[int] = set()
    for raw_slot in raw_slots:
        raw_slot, _ = normalize_ascii_integer(raw_slot)
        if isinstance(raw_slot, bool) or not isinstance(raw_slot, int):
            return None, 'assembly_result_consumed_slot_not_integer'
        if raw_slot < 1 or raw_slot > slot_count:
            return None, 'assembly_result_consumed_slot_out_of_range'
        if raw_slot in seen_slots:
            return None, 'assembly_result_consumed_slot_duplicate'
        seen_slots.add(raw_slot)
        consumed_slots.append(raw_slot)
    consumed_slots.sort()
    if unreasonable_input and consumed_slots != list(range(1, slot_count + 1)):
        return None, 'assembly_result_unreasonable_requires_all_slots'
    if not unreasonable_input and slot_count > 0 and len(consumed_slots) == slot_count:
        return None, 'assembly_result_ordinary_failure_cannot_consume_all_slots'

    return {
        'success': False,
        'new_name': '',
        'new_type': '',
        'new_desc': '',
        'new_rarity': '',
        'narrative': narrative.strip(),
        'consumed_slots': consumed_slots,
        'unreasonable_input': unreasonable_input,
    }, None
