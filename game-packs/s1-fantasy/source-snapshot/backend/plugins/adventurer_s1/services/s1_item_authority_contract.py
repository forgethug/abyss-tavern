"""构装产物资格判定：内部结果只决定 AI 资产投影，不改玩家物品。"""

import hashlib
import json


ITEM_AUTHORITY_REVIEW_KEY = 'assembly_authority_review'
ITEM_AUTHORITY_REVIEW_VERSION = 1
ITEM_AUTHORITY_DECISIONS = frozenset({'no_claim', 'supported', 'unsupported'})

ITEM_AUTHORITY_SYSTEM_PROMPT = """你负责判定一件构装成品所声称的转职或血脉变更资格是否成立。

只审查成品是否自行赋予转职资格或种族/血脉变更；与这些资格无关的功能不因稀有或强大而自动进入本次判定范围。
资格应有与其内容相称的来源：转职所需的实际知识、传承或有效授予关系，以及足以造成对应血脉变化的实体与作用机制。投入物允许通过构装形成合理的新功能，不要求成品照抄材料；材料的储能、保存、稳定或纪念性质不能自行建立具体传承、与转职绑定的组织授权或目标血脉。
以冻结投入全文、成品全文和服务端提供的来源链共同判断。沿用旧构装成品上的资格文字不等于该资格已经成立；来源链中已判无依据的主张不能因更名、升级或再次构装而变成依据。旧成品仍可提供原本成立的物理材料或有限功能，有独立新依据时按新依据判断。official=true 只表示服务端确认的正式凭证；名称、自称、稀有度和旧生成/一般构装审核通过都不构成官方证明。
来源缺页时不虚构缺失事实；只有资格主张自身缺少充分依据才判 unsupported，普通物品仍可判 no_claim。你不批准实际转职、血脉写回、消耗或删除，也不审核构装整体成败。

只返回 JSON 对象，字段语义如下：
input_hash：原样回传本次冻结输入的 input_hash，绑定唯一候选。
decision：no_claim 表示成品没有上述资格主张；supported 表示存在主张且有相称来源；unsupported 表示存在主张但来源不能支持。三者互斥且必须选择一个。
reason：非空文字，说明本次判定的具体因果依据，仅供内部审计。
evidence_refs：引用输入 evidence_refs 中实际支撑判定的准确引用数组，不得自创、重复或使用名称代替引用。supported 至少引用一项；其余可为空。
返回这些字段的完整对象即可。不要提供改写后的物品、剧情、对玩家的解释或任何执行指令。
"""


def authority_input_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def build_item_authority_payload(*, input_slots: list, candidate: dict,
                                 provenance: dict) -> dict:
    slots = [{
        'ref': f'slot:{index}',
        'name': row.get('name'),
        'type': row.get('type'),
        'rarity': row.get('rarity'),
        'description': row.get('description', row.get('desc')),
    } for index, row in enumerate(input_slots, 1)]
    payload = {
        'version': ITEM_AUTHORITY_REVIEW_VERSION,
        'input_slots': slots,
        'candidate': {
            'name': candidate.get('new_name'),
            'type': candidate.get('new_type'),
            'rarity': candidate.get('new_rarity'),
            'description': candidate.get('new_desc'),
        },
        'provenance': provenance,
        'evidence_refs': [row['ref'] for row in slots] + [
            row['ref'] for row in provenance.get('origins', [])
        ],
    }
    payload['input_hash'] = authority_input_hash(payload)
    return payload


def normalize_item_authority_review(raw, payload: dict) -> tuple[dict | None, str | None]:
    """只接纳当前冻结候选的裁决；忽略所有未消费字段。"""
    if not isinstance(raw, dict):
        return None, 'item_authority_review_not_object'
    if raw.get('input_hash') != payload.get('input_hash'):
        return None, 'item_authority_review_identity_mismatch'
    decision = raw.get('decision')
    if not isinstance(decision, str) or decision not in ITEM_AUTHORITY_DECISIONS:
        return None, 'item_authority_review_decision_invalid'
    reason = raw.get('reason')
    refs = raw.get('evidence_refs')
    allowed = set(payload.get('evidence_refs') or [])
    if (not isinstance(refs, list)
            or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
            or len(set(refs)) != len(refs)
            or (decision == 'supported' and not refs)):
        return None, 'item_authority_review_evidence_invalid'
    return {
        'version': ITEM_AUTHORITY_REVIEW_VERSION,
        'input_hash': payload['input_hash'],
        'decision': decision,
        'reason': (reason.strip() if isinstance(reason, str) and reason.strip()
                   else f'资格判定：{decision}；模型未提供有效内部说明。'),
        'evidence_refs': list(refs),
    }, None


def item_authority_excludes_ai(use_condition) -> bool:
    review = use_condition.get(ITEM_AUTHORITY_REVIEW_KEY) if isinstance(use_condition, dict) else None
    return bool(isinstance(review, dict)
                and review.get('version') == ITEM_AUTHORITY_REVIEW_VERSION
                and review.get('decision') == 'unsupported')
