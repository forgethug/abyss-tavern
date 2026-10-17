"""S1 数据申诉：按 expedition_log_id 缓存副本事实卡，减少重复全文阅读。"""
import json
import logging
import math
import re
import requests
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, get_s1_ai_response_metadata
from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases as post_s1_ai_request, with_player_name_aliases
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiResponseContractResult, parse_s1_ai_http_json_response, parse_s1_ai_json_content
from plugins.adventurer_s1.models import AdventurerS1AppealFactSheet
from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_DATA_APPEAL_SHARED_CACHE_PREFIX
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_finite_number, normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.s1_mimo_client_config import get_mimo_compatible_client_config
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
logger = logging.getLogger(__name__)
_CHINESE_TEXT_RE = re.compile('[\\u3400-\\u9fff]')
from plugins.adventurer_s1.services.s1_appeal_hidden_drop_guard import HIDDEN_DROP_SOURCE_RULE
_FACT_SHEET_SCHEMA_VERSION = 3
_FACT_SHEET_TOP_LEVEL_FIELDS = frozenset({'schema_version', 'summary', 'members', 'team_assets', 'unique_assets', 'ownership_conflicts', 'appeal_relevant_flags', 'subsidy_assessment', 'warnings'})
_FACT_SHEET_FLAG_FIELDS = frozenset({'possible_missing_item_grants', 'possible_missing_skill_grants', 'possible_missing_trait_grants', 'possible_missing_gold_grants', 'possible_wrong_consumptions', 'possible_old_trait_reactivation', 'possible_team_pool', 'possible_tavern_subsidy'})
_FACT_SHEET_EXPEDITION_EVIDENCE_FIELDS = ('all_events', 'member_update', 'party_member_updates_summary', 'hidden_dungeon_reward_receipts')
_FACT_SHEET_SYSTEM_PROMPT = S1_DATA_APPEAL_SHARED_CACHE_PREFIX + HIDDEN_DROP_SOURCE_RULE + '\n\n' + '你是深渊酒馆 S1 数据申诉的副本事实书记员。你的任务是只读一次完整探险记录，产出后续多名队员申诉可复用的结构化证据卡。\n\n你不是在裁决某个玩家的申诉，也不得输出任何可落库 patch。你要把全文压缩成“可追溯证据索引”：谁在第几段获得/消耗/转交了什么，数量是多少，最终是否保留，是否存在归属冲突或常见申诉风险。后续陪审团 1 号/2 号会按各自玩家数据独立判断。\n\n硬规则：\n1. 只根据 all_events、member_update、party_member_updates_summary、hidden_dungeon_reward_receipts、party_roster、team_pool_status 和 tavern_subsidy_status 记录事实；不要采信未来玩家口述，也不要读取面向具体申诉者的其它证据。独立掉落回执按【隐藏副本独立掉落归属】核对，不为其补造正文事件引用。\n1a. all_events 每项是 `{"event_index":0,"text":"..."}`；event_index 是后端固定的零基事件引用并与原数组位置相同。所有 evidence_events / negated_by_events 只能抄写实际给出的 event_index，禁止按自然段、显示序号或自行重排后的顺序编号。\n下列2—6只约束从正文抽取的事件事实。隐藏掉落回执是独立结算权威，仅用于 settlement_notes 与排除隐藏份额的误报；不将回执改写成 item_events、ownership_conflicts，不为其生成 evidence_events 或 evidence_quote，也不因没有正文索引而降级为 warnings。\n2. 明确区分个人所有、团队保管、任务交付前暂存、临时持有、队友代携与最终转赠；只有正文确认永久归某成员所有，才能把该成员记为 owner。“负责保管/拿着/代为携带”本身不是个人所有证据。成员已有装备、收藏、盒子、箱子、仓储能力或其它容器也只能说明归属确定后的存放能力，不能反向证明新战利品归该成员。若剧情确认的最终所有权与 member_update/结算摘要冲突，必须写入 ownership_conflicts，不能只把结算 owner 写成最终真相。\n3. 唯一、点名、独一遗物/武器/法器只能给一个 owner；常见药水/箭矢/草药等可量产物资可多人各得。\n4. 涉及物品/材料/药水/箭矢/金币/钱袋/赏金时必须保留数量信息：能确定就填 quantity 数字；只有文字数量或模糊数量时令 quantity=null，并把原文数量措辞逐字保存在 quantity_text，不得概括成另一种范围或只写“模糊”；正文已明确实际取得但完全省略件数时令 quantity=null、quantity_text="正文未写数量"；连是否实际取得都不明确时才写 quantity=null、quantity_text="未知"。不要把数量只藏在 summary 里。容器容量与物品内部组成不是所得库存单位数。\n5. 每条收益/损失/转交事实都要写 final_state：kept|stored|equipped|party_custody|delivery_pending|temporary_hold|consumed|lost|transferred|spent|upgraded|unclear。只有个人所有权成立且全文没有后续否定，才写 kept/stored/equipped；团队保管、待交付和临时持有使用各自状态。后续被消耗、丢弃、转交或损坏时必须记录 negated_by_events。\n6. 每条事实都要写 evidence_events（all_events 下标，从 0 开始）和 evidence_quote（极短原文依据）。如果无法定位段落，不得把它当强事实，放 warnings。\n7. 不要给出具体最终补发 patch；只给 evidence 和 risk。\n8. 酒馆补贴只给区间与理由：低收益+完成任务、重伤/消耗、净收益明显偏低等；不要承诺一定发放。\n9. 技能与特质事实分类：技能是角色本人已学会、领悟、训练完成且日后可主动复现的招式、法术、工艺、战斗方法或职业能力，归入 skill_events；特质是稳定体质、心理状态、诅咒/祝福烙印、后遗症、身份印记、长期倾向或被动状态，归入 trait_events。临时祝福、卷轴外力、场地效果、NPC/队友代施法若未写明永久掌握或稳定内化，只能记为 temporary/mentioned，不要标成 learned 或 active 特质。\n10. 输出必须是纯 JSON，字段尽量短但不能丢关键数量、归属、最终状态和段落索引。\n\n输出格式：\n{\n  "schema_version": 3,\n  "summary": "<副本整体经过与结果，120字内>",\n  "members": [],\n  "team_assets": [],\n  "unique_assets": [],\n  "ownership_conflicts": [],\n  "appeal_relevant_flags": {\n    "possible_missing_item_grants": [],\n    "possible_missing_skill_grants": [],\n    "possible_missing_trait_grants": [],\n    "possible_missing_gold_grants": [],\n    "possible_wrong_consumptions": [],\n    "possible_old_trait_reactivation": [],\n    "possible_team_pool": [],\n    "possible_tavern_subsidy": []\n  },\n  "subsidy_assessment": {"difficulty_vs_reward":"unclear", "team_subsidy_reasonable": false, "suggested_total_range":[0,0], "notes":"<中文理由>"},\n  "warnings": []\n}\nmembers 每项必须包含 adventurer_id、name、role_summary、item_events、trait_events、skill_events、gold_events、hp_events、settlement_notes。每类事件只使用上文规定的 action/ownership/final_state/severity 枚举，并包含 evidence_events 与 evidence_quote；有后续否定时同时填 negated_by_events。team_assets、unique_assets、ownership_conflicts 也必须保留归属、最终状态、证据段落与极短证据原文。\n'

def _compact_fact_sheet_party_roster(party_roster: list | None) -> list:
    """事实卡只需 id + 姓名对照，省略其余成员字段。"""
    slim: list[dict] = []
    for row in party_roster or []:
        if not isinstance(row, dict):
            continue
        entry: dict = {}
        for key in ('adventurer_id', 'id', 'name', 'adventurer_name'):
            if key in row and row[key] is not None:
                entry[key] = row[key]
        if entry:
            slim.append(entry)
    return slim

def _compact_fact_sheet_status_block(block: dict | None) -> dict:
    if not isinstance(block, dict):
        return {}
    return {k: v for k, v in block.items() if v not in (None, '', [], {})}

def _build_fact_sheet_evidence_payload(*, expedition_log_id: int, expedition_evidence: dict) -> dict:
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    projected_evidence = {key: expedition_evidence[key] for key in _FACT_SHEET_EXPEDITION_EVIDENCE_FIELDS if key in expedition_evidence}
    return AdventurerS1DataAppealService.prepare_appeal_ai_user_payload({'expedition_log_id': expedition_log_id, 'expedition_evidence': projected_evidence})

def _build_fact_sheet_context_payload(*, party_roster: list | None, team_pool_status: dict | None, tavern_subsidy_status: dict | None) -> dict:
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    return AdventurerS1DataAppealService.prepare_appeal_ai_user_payload({'party_roster': _compact_fact_sheet_party_roster(party_roster), 'team_pool_status': _compact_fact_sheet_status_block(team_pool_status), 'tavern_subsidy_status': _compact_fact_sheet_status_block(tavern_subsidy_status)})

def _parse_json(content: str) -> dict | None:
    text = str(content or '').strip()
    if '```' in text:
        start, end = (text.find('{'), text.rfind('}'))
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        return parsed if isinstance(parsed, dict) else None
    result = parse_s1_ai_json_content(content, repair_mode='lossless_syntax_only')
    return result.data if result.ok else None

def _valid_event_indices(value: object, *, allow_empty: bool=False) -> bool:
    if not isinstance(value, list) or (not allow_empty and (not value)):
        return False
    normalized: list[int] = []
    for item in value:
        item, _ = normalize_ascii_integer(item, minimum=0)
        if type(item) is not int or item < 0:
            return False
        normalized.append(item)
    value[:] = normalized
    return True

def _normalize_optional_positive_integer(value: object) -> tuple[object, bool]:
    if value is None:
        return (None, True)
    normalized, _ = normalize_ascii_integer(value, minimum=1)
    return (normalized, type(normalized) is int and normalized > 0)

def _normalize_number_range(value: object) -> list[int | float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    normalized_range: list[int | float] = []
    for item in value:
        normalized, _ = normalize_ascii_integer(item)
        if type(normalized) is not int:
            normalized, _ = normalize_ascii_finite_number(item)
            if isinstance(normalized, bool) or not isinstance(normalized, (int, float)) or (type(normalized) is float and (not math.isfinite(normalized))):
                return None
        normalized_range.append(normalized)
    return normalized_range

def _validate_evidence_event_rows(rows: object, *, exact_fields: frozenset, enum_fields: dict[str, frozenset], require_name: bool=True) -> bool:
    if not isinstance(rows, list):
        return False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return False
        row = {key: row[key] for key in exact_fields if key in row}
        rows[index] = row
        if frozenset(row) != exact_fields:
            return False
        if require_name and (not isinstance(row.get('name'), str) or not row['name'].strip()):
            return False
        if not _valid_event_indices(row.get('evidence_events')):
            return False
        if not isinstance(row.get('evidence_quote'), str) or not row['evidence_quote'].strip():
            return False
        if 'negated_by_events' in row and (not _valid_event_indices(row.get('negated_by_events'), allow_empty=True)):
            return False
        for field, allowed in enum_fields.items():
            if row.get(field) not in allowed:
                return False
    return True

def _validate_fact_sheet_member(row: object) -> bool:
    member_fields = frozenset({'adventurer_id', 'name', 'role_summary', 'item_events', 'trait_events', 'skill_events', 'gold_events', 'hp_events', 'settlement_notes'})
    if not isinstance(row, dict):
        return False
    projected = {key: row[key] for key in member_fields if key in row}
    row.clear()
    row.update(projected)
    if frozenset(row) != member_fields:
        return False
    adventurer_id, valid_adventurer_id = _normalize_optional_positive_integer(row.get('adventurer_id'))
    if not valid_adventurer_id or adventurer_id is None:
        return False
    row['adventurer_id'] = adventurer_id
    for field in ('name', 'role_summary', 'settlement_notes'):
        if not isinstance(row.get(field), str):
            return False
    item_fields = frozenset({'name', 'quantity', 'quantity_text', 'type_hint', 'rarity_hint', 'action', 'ownership', 'from_adventurer_id', 'to_adventurer_id', 'final_state', 'evidence_events', 'evidence_quote', 'negated_by_events'})
    if not _validate_evidence_event_rows(row.get('item_events'), exact_fields=item_fields, enum_fields={'action': frozenset({'gained', 'lost', 'consumed', 'transferred', 'borrowed', 'upgraded', 'stored'}), 'ownership': frozenset({'self', 'teammate', 'team', 'unclear'}), 'final_state': frozenset({'kept', 'stored', 'equipped', 'party_custody', 'delivery_pending', 'temporary_hold', 'consumed', 'lost', 'transferred', 'upgraded', 'unclear'})}):
        return False
    for item in row['item_events']:
        quantity, quantity_valid = _normalize_optional_positive_integer(item.get('quantity'))
        if not quantity_valid:
            return False
        item['quantity'] = quantity
        if not isinstance(item.get('quantity_text'), str):
            return False
        if not isinstance(item.get('type_hint'), str) or not isinstance(item.get('rarity_hint'), str):
            return False
        for field in ('from_adventurer_id', 'to_adventurer_id'):
            member_id, member_id_valid = _normalize_optional_positive_integer(item.get(field))
            if not member_id_valid:
                return False
            item[field] = member_id
    trait_fields = frozenset({'name', 'action', 'final_state', 'evidence_events', 'evidence_quote', 'old_injury_reactivation'})
    if not _validate_evidence_event_rows(row.get('trait_events'), exact_fields=trait_fields, enum_fields={'action': frozenset({'gained', 'removed', 'worsened', 'recovered', 'mentioned'}), 'final_state': frozenset({'active', 'removed', 'unclear'})}):
        return False
    for item in row['trait_events']:
        old_injury_reactivation, _ = normalize_exact_boolean(item.get('old_injury_reactivation'))
        if type(old_injury_reactivation) is not bool:
            return False
        item['old_injury_reactivation'] = old_injury_reactivation
    skill_fields = frozenset({'name', 'action', 'final_state', 'evidence_events', 'evidence_quote'})
    if not _validate_evidence_event_rows(row.get('skill_events'), exact_fields=skill_fields, enum_fields={'action': frozenset({'learned', 'used_temporarily', 'mentioned'}), 'final_state': frozenset({'learned', 'temporary', 'unclear'})}):
        return False
    gold_fields = frozenset({'name', 'quantity', 'quantity_text', 'estimated_gold_range', 'ownership', 'final_state', 'evidence_events', 'evidence_quote', 'negated_by_events'})
    if not _validate_evidence_event_rows(row.get('gold_events'), exact_fields=gold_fields, enum_fields={'ownership': frozenset({'self', 'team', 'unclear'}), 'final_state': frozenset({'kept', 'party_custody', 'delivery_pending', 'spent', 'lost', 'unclear'})}):
        return False
    for item in row['gold_events']:
        quantity, quantity_valid = _normalize_optional_positive_integer(item.get('quantity'))
        if not quantity_valid:
            return False
        item['quantity'] = quantity
        if not isinstance(item.get('quantity_text'), str):
            return False
        gold_range = _normalize_number_range(item.get('estimated_gold_range'))
        if gold_range is None:
            return False
        item['estimated_gold_range'] = gold_range
    hp_fields = frozenset({'severity', 'evidence_events', 'evidence_quote'})
    return _validate_evidence_event_rows(row.get('hp_events'), exact_fields=hp_fields, enum_fields={'severity': frozenset({'minor', 'medium', 'heavy', 'dead', 'unclear'})}, require_name=False)

def _validate_fact_sheet(fact_sheet: object) -> dict | None:
    if not isinstance(fact_sheet, dict):
        return None
    fact_sheet = {key: fact_sheet[key] for key in _FACT_SHEET_TOP_LEVEL_FIELDS if key in fact_sheet}
    if frozenset(fact_sheet) != _FACT_SHEET_TOP_LEVEL_FIELDS:
        return None
    schema_version, _ = normalize_ascii_integer(fact_sheet.get('schema_version'), minimum=_FACT_SHEET_SCHEMA_VERSION, maximum=_FACT_SHEET_SCHEMA_VERSION)
    if type(schema_version) is not int or schema_version != _FACT_SHEET_SCHEMA_VERSION:
        return None
    fact_sheet['schema_version'] = schema_version
    summary = fact_sheet.get('summary')
    members = fact_sheet.get('members')
    warnings = fact_sheet.get('warnings')
    if not isinstance(summary, str) or not summary.strip() or (not _CHINESE_TEXT_RE.search(summary)) or (not isinstance(members, list)) or any((not _validate_fact_sheet_member(row) for row in members)) or (not isinstance(warnings, list)) or any((not isinstance(item, str) or not item.strip() for item in warnings)):
        return None
    team_fields = frozenset({'name', 'type', 'quantity', 'quantity_text', 'ownership', 'final_state', 'estimated_gold_range', 'evidence_events', 'evidence_quote', 'negated_by_events'})
    if not _validate_evidence_event_rows(fact_sheet.get('team_assets'), exact_fields=team_fields, enum_fields={'type': frozenset({'gold', 'item', 'other'}), 'ownership': frozenset({'team_pool', 'unclear'}), 'final_state': frozenset({'kept', 'spent', 'lost', 'unclear'})}):
        return None
    for row in fact_sheet['team_assets']:
        quantity, quantity_valid = _normalize_optional_positive_integer(row.get('quantity'))
        if not quantity_valid or not isinstance(row.get('quantity_text'), str):
            return None
        row['quantity'] = quantity
        value_range = _normalize_number_range(row.get('estimated_gold_range'))
        if value_range is None:
            return None
        row['estimated_gold_range'] = value_range
    unique_fields = frozenset({'name', 'owner_adventurer_id', 'owner_name', 'final_state', 'evidence_events', 'evidence_quote', 'negated_by_events'})
    if not _validate_evidence_event_rows(fact_sheet.get('unique_assets'), exact_fields=unique_fields, enum_fields={'final_state': frozenset({'kept', 'stored', 'equipped', 'party_custody', 'delivery_pending', 'temporary_hold', 'transferred', 'lost', 'unclear'})}):
        return None
    for row in fact_sheet['unique_assets']:
        owner_id, owner_id_valid = _normalize_optional_positive_integer(row.get('owner_adventurer_id'))
        if not owner_id_valid:
            return None
        row['owner_adventurer_id'] = owner_id
        if not isinstance(row.get('owner_name'), str):
            return None
    conflict_fields = frozenset({'name', 'issue', 'from_adventurer_id', 'to_adventurer_id', 'evidence_events', 'evidence_quote'})
    if not _validate_evidence_event_rows(fact_sheet.get('ownership_conflicts'), exact_fields=conflict_fields, enum_fields={'issue': frozenset({'temporary_borrow_may_be_settled_as_transfer', 'story_owner_settled_to_teammate', 'custody_misread_as_personal_owner', 'unclear_owner', 'duplicate_unique_claim'})}):
        return None
    for row in fact_sheet['ownership_conflicts']:
        for field in ('from_adventurer_id', 'to_adventurer_id'):
            member_id, member_id_valid = _normalize_optional_positive_integer(row.get(field))
            if not member_id_valid:
                return None
            row[field] = member_id
    flags = fact_sheet.get('appeal_relevant_flags')
    if not isinstance(flags, dict):
        return None
    flags = {key: flags[key] for key in _FACT_SHEET_FLAG_FIELDS if key in flags}
    fact_sheet['appeal_relevant_flags'] = flags
    if frozenset(flags) != _FACT_SHEET_FLAG_FIELDS:
        return None
    if any((not isinstance(value, list) or any((not isinstance(item, str) or not item.strip() for item in value)) for value in flags.values())):
        return None
    subsidy = fact_sheet.get('subsidy_assessment')
    subsidy_fields = frozenset({'difficulty_vs_reward', 'team_subsidy_reasonable', 'suggested_total_range', 'notes'})
    if not isinstance(subsidy, dict):
        return None
    subsidy = {key: subsidy[key] for key in subsidy_fields if key in subsidy}
    fact_sheet['subsidy_assessment'] = subsidy
    if frozenset(subsidy) != subsidy_fields:
        return None
    if subsidy.get('difficulty_vs_reward') not in {'balanced', 'low_reward', 'low_reward_high_cost', 'unclear'}:
        return None
    team_subsidy_reasonable, _ = normalize_exact_boolean(subsidy.get('team_subsidy_reasonable'))
    if type(team_subsidy_reasonable) is not bool:
        return None
    subsidy['team_subsidy_reasonable'] = team_subsidy_reasonable
    subsidy_range = _normalize_number_range(subsidy.get('suggested_total_range'))
    if subsidy_range is None:
        return None
    subsidy['suggested_total_range'] = subsidy_range
    if not isinstance(subsidy.get('notes'), str):
        return None
    return fact_sheet

def _is_current_fact_sheet(fact_sheet: object) -> bool:
    return _validate_fact_sheet(fact_sheet) is not None

def _derive_fact_sheet_frozen_scope(*, evidence_payload: dict, context_payload: dict) -> tuple[set[int], set[int]]:
    """Derive the exact event/member scope visible in this generation request."""
    frozen_evidence = evidence_payload.get('expedition_evidence')
    frozen_evidence = frozen_evidence if isinstance(frozen_evidence, dict) else {}
    all_events = frozen_evidence.get('all_events')
    valid_event_indices = set(range(len(all_events))) if isinstance(all_events, list) else set()
    valid_member_ids: set[int] = set()

    def _add_member_rows(rows: object) -> None:
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id = row.get('adventurer_id') if row.get('adventurer_id') is not None else row.get('id')
            member_id, _ = normalize_ascii_integer(raw_id, minimum=1)
            if type(member_id) is int and member_id > 0:
                valid_member_ids.add(member_id)
    _add_member_rows(context_payload.get('party_roster'))
    _add_member_rows(frozen_evidence.get('member_update'))
    _add_member_rows(frozen_evidence.get('member_updates'))
    _add_member_rows(frozen_evidence.get('party_member_updates_summary'))
    return (valid_event_indices, valid_member_ids)

def _validate_fact_sheet_frozen_scope(fact_sheet: object, *, valid_event_indices: set[int], valid_member_ids: set[int]) -> bool:
    """Validate every evidence/member reference against this request's frozen input."""
    if not isinstance(fact_sheet, dict) or not valid_member_ids or any((type(member_id) is not int or member_id <= 0 for member_id in valid_member_ids)):
        return False
    members = fact_sheet.get('members')
    if not isinstance(members, list):
        return False
    seen_member_ids: set[int] = set()
    for row in members:
        if not isinstance(row, dict):
            return False
        member_id = row.get('adventurer_id')
        if type(member_id) is not int or member_id not in valid_member_ids or member_id in seen_member_ids:
            return False
        seen_member_ids.add(member_id)
    if seen_member_ids != valid_member_ids:
        return False
    event_fields = frozenset({'evidence_events', 'negated_by_events'})
    optional_member_fields = frozenset({'from_adventurer_id', 'to_adventurer_id', 'owner_adventurer_id'})

    def _walk(value: object) -> bool:
        if isinstance(value, list):
            return all((_walk(item) for item in value))
        if not isinstance(value, dict):
            return True
        for key, item in value.items():
            if key in event_fields:
                if not isinstance(item, list) or any((type(event_index) is not int or event_index not in valid_event_indices for event_index in item)):
                    return False
            elif key == 'adventurer_id':
                if type(item) is not int or item not in valid_member_ids:
                    return False
            elif key in optional_member_fields:
                if item is not None and (type(item) is not int or item not in valid_member_ids):
                    return False
            elif not _walk(item):
                return False
        return True
    return isinstance(fact_sheet, dict) and _walk(fact_sheet)

@with_player_name_aliases
def _call_fact_sheet_ai(appeal_id: int, *, expedition_log_id: int, expedition_evidence: dict, party_roster: list | None=None, team_pool_status: dict | None=None, tavern_subsidy_status: dict | None=None) -> tuple[dict | None, str | None]:
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('appeal_fact_sheet')
    except AIProfileAssignmentError as exc:
        logger.warning('申诉事实卡 AI 用途路由不可用: %s', exc)
        return (None, None)
    if not api_key or not api_base:
        logger.warning('申诉事实卡 AI 跳过：API key 或 base URL 未配置')
        return (None, None)
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    evidence, _mapping = AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai(expedition_evidence)
    evidence_payload = _build_fact_sheet_evidence_payload(expedition_log_id=expedition_log_id, expedition_evidence=evidence)
    context_payload = _build_fact_sheet_context_payload(party_roster=party_roster, team_pool_status=team_pool_status, tavern_subsidy_status=tavern_subsidy_status)
    valid_event_indices, valid_member_ids = _derive_fact_sheet_frozen_scope(evidence_payload=evidence_payload, context_payload=context_payload)
    runtime_model = model
    usage = None
    usage_recorded = False
    request_metadata: dict[str, object] = {}

    def _record_call_usage(*, is_success: bool, request_exception: BaseException | None=None) -> None:
        nonlocal usage_recorded
        if usage_recorded or (not usage and request_exception is None):
            return
        record_token_usage(call_type='data_appeal_fact_sheet_ai', usage=usage, model=runtime_model, expedition_log_id=expedition_log_id, appeal_id=appeal_id, attempt_number=1, is_success=is_success, failure_stage=None if is_success else 'http_or_response', validation_error_code=None if request_exception is None else f'http_or_response.{type(request_exception).__name__}', record_without_usage=request_exception is not None, request_metadata=request_metadata, request_exception=request_exception)
        usage_recorded = True

    def validate_fact_sheet_response(decoded) -> S1AiResponseContractResult:
        parsed_candidate = _validate_fact_sheet(decoded.json_result.data)
        if parsed_candidate is None:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'fact_sheet_invalid', failure_stage='response_parse' if decoded.json_result.error_code else 'contract_validation')
        if not _validate_fact_sheet_frozen_scope(parsed_candidate, valid_event_indices=valid_event_indices, valid_member_ids=valid_member_ids):
            return S1AiResponseContractResult.reject('fact_sheet_frozen_scope_invalid', failure_stage='contract_validation')
        return S1AiResponseContractResult.accept()
    try:
        db_manager.remove()
        response = post_s1_ai_request(f'{api_base}/chat/completions', feature='appeal_fact_sheet', request_metadata_out=request_metadata, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': _FACT_SHEET_SYSTEM_PROMPT}, {'role': 'user', 'content': json.dumps(evidence_payload, ensure_ascii=False, separators=(',', ':'))}, {'role': 'user', 'content': json.dumps(context_payload, ensure_ascii=False, separators=(',', ':'))}], 'temperature': 0.1}, timeout=timeout, response_validator=validate_fact_sheet_response)
        decoded = parse_s1_ai_http_json_response(response)
        usage = decoded.http_result.usage
        runtime_model = str(get_s1_ai_response_metadata(response).get('model') or model)
        parsed = _validate_fact_sheet(decoded.json_result.data)
        if parsed is None:
            raise ValueError('事实卡 AI 未返回有效 JSON')
        if not _validate_fact_sheet_frozen_scope(parsed, valid_event_indices=valid_event_indices, valid_member_ids=valid_member_ids):
            raise ValueError('事实卡 AI 引用了冻结剧情或队伍范围之外的 ID')
        _record_call_usage(is_success=True)
        return (parsed, runtime_model)
    except Exception as exc:
        logger.warning('申诉事实卡 AI 调用失败 appeal=%s expedition=%s: %s', appeal_id, expedition_log_id, exc)
        _record_call_usage(is_success=False, request_exception=exc)
        return (None, runtime_model)

def get_or_create_appeal_fact_sheet(appeal_id: int, *, expedition_log_id: int | None, expedition_evidence: dict | None, party_roster: list | None=None, team_pool_status: dict | None=None, tavern_subsidy_status: dict | None=None) -> dict | None:
    """返回同副本共享事实卡；生成失败时返回 None，由调用方回退旧全文模式。"""
    if not expedition_log_id or not isinstance(expedition_evidence, dict):
        return None
    row = AdventurerS1AppealFactSheet.query.filter_by(expedition_log_id=int(expedition_log_id)).first()
    if row and _is_current_fact_sheet(row.fact_sheet):
        return row.fact_sheet
    fact_sheet, model = _call_fact_sheet_ai(appeal_id, expedition_log_id=int(expedition_log_id), expedition_evidence=expedition_evidence, party_roster=party_roster, team_pool_status=team_pool_status, tavern_subsidy_status=tavern_subsidy_status)
    if not isinstance(fact_sheet, dict):
        return None
    try:
        db_manager.remove()
        row = AdventurerS1AppealFactSheet.query.filter_by(expedition_log_id=int(expedition_log_id)).first()
        if row and _is_current_fact_sheet(row.fact_sheet):
            return row.fact_sheet
        if row:
            row.model = model
            row.fact_sheet = fact_sheet
            row.raw_response = json.dumps(fact_sheet, ensure_ascii=False)
        else:
            row = AdventurerS1AppealFactSheet(expedition_log_id=int(expedition_log_id), model=model, fact_sheet=fact_sheet, raw_response=json.dumps(fact_sheet, ensure_ascii=False))
            ok, err = db_manager.add(row, flush=False)
            if not ok:
                logger.warning('申诉事实卡写入失败 expedition=%s: %s', expedition_log_id, err)
                return fact_sheet
        ok, err = db_manager.commit()
        if not ok:
            logger.warning('申诉事实卡提交失败 expedition=%s: %s', expedition_log_id, err)
        return fact_sheet
    except Exception as exc:
        logger.warning('申诉事实卡保存异常 expedition=%s: %s', expedition_log_id, exc)
        return fact_sheet
