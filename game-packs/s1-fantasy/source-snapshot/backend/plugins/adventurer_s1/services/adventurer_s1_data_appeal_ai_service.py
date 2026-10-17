"""深渊酒馆 S1 数据申诉 AI：生成受后端白名单约束的数据修复 patch。"""
import json
from plugins.adventurer_s1.services.s1_appeal_hidden_drop_guard import HIDDEN_DROP_APPEAL_RULE
from plugins.adventurer_s1.services.s1_team_loot_contract import TEAM_LOOT_APPEAL_RULE, TEAM_LOOT_STRUCTURE_RULE, project_team_loot
from plugins.adventurer_s1.services.s1_description_repair_contract import S1_MISSING_DESCRIPTION_REPAIR_RULES
import logging
import re
import requests
from flask import current_app, has_app_context
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, enrich_s1_ai_response_metadata, get_s1_ai_response_metadata, log_s1_ai_response_outcome
from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases as post_s1_ai_request, with_player_name_aliases
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiResponseContractResult, build_s1_ai_raw_excerpt, parse_s1_ai_http_json_response
from plugins.adventurer_s1.services.s1_mimo_client_config import get_mimo_compatible_client_config
from plugins.adventurer_s1.services.s1_data_appeal_input_service import S1_APPEAL_DESCRIPTION_INPUT_RULE, pack_appeal_description_inputs
from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_DATA_APPEAL_PLAYER_DIRECTED_CONTENT_RULES, S1_DATA_APPEAL_PRIMARY_CONTENT_OUTLET, S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES, S1_DATA_APPEAL_RESOLUTION_BASES, S1_DATA_APPEAL_RESPONSIBILITY_CONTRACT, S1_DATA_APPEAL_DATA_FIX_SCOPE, S1_DATA_APPEAL_FACT_SHEET_INPUT_RULE, S1_DATA_APPEAL_TRAIT_CONSOLIDATION_RULES, S1_DATA_APPEAL_SHARED_CACHE_PREFIX, S1_DATA_APPEAL_SHARED_EVIDENCE_PREFIX_CONFIG, S1_ECONOMIC_RESOURCE_SOURCE_RULES, S1_SKILL_RESOURCE_AUTHORITY_BOUNDARY, build_s1_data_appeal_messages, log_s1_data_appeal_cache_diagnostics
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage, s1_ai_usage_has_positive_tokens
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_destructive_contract import DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS, get_data_appeal_destructive_action_max, project_valid_data_appeal_destructive_actions
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import normalize_data_appeal_currency_adjustment
logger = logging.getLogger(__name__)
_CHINESE_TEXT_RE = re.compile('[\\u3400-\\u9fff]')
_APPEAL_CHECKLIST_CATEGORIES = frozenset({'gold', 'subsidy', 'quantity', 'merge', 'item', 'desc', 'trait', 'skill', 'other'})
_APPEAL_CHECKLIST_STATUSES = frozenset({'done', 'partial', 'rejected', 'pending'})
_APPEAL_CHECKLIST_RESOLUTION_BASES_BY_STATUS = {'done': frozenset({'repair_supported', 'already_satisfied_same_case'}), 'partial': frozenset({'partially_supported'}), 'rejected': S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES, 'pending': frozenset({'lookup_pending'})}
DATA_APPEAL_RECORD_SECTIONS = ('traits', 'equipment', 'warehouse', 'skills')
_APPEAL_RECORD_SECTIONS = frozenset(DATA_APPEAL_RECORD_SECTIONS)
_APPEAL_LOOKUP_SECTION_ALIASES = {'trait': 'traits', 'skill': 'skills', 'equipments': 'equipment', 'warehouses': 'warehouse'}
DATA_APPEAL_LOOKUP_STRUCTURE_RULE = 'lookup_queries 是对象数组，每项仅含 type 与 name；type 使用 traits、equipment、warehouse、skills 之一，name 使用待核对条目的准确完整名称。'
DATA_APPEAL_PATCH_RECORD_SECTION_STRUCTURE_RULE = '任何 patch 对象（含 patch、supplement_patch、override_patch）中的 traits、equipment、warehouse、skills 如出现，section 值必须是 JSON 对象；对象内 update 与 delete 如出现必须是数组，禁止把 section 值直接写成数组。空 update/delete 或空 section 应省略；其它 patch section 继续遵守各自既定对象或数组类型。记录 section 的动作层级固定为 section.update[] / section.delete[]；不得把 section 本身直接写成记录数组。'
DATA_APPEAL_CURRENCY_ADJUSTMENT_STRUCTURE_RULE = 'currency_adjustments 是数组，每项使用 {"currency_code":"gold","amount":<本次补发的正数金额>,"reason":"<权威证据与补发依据>"}。currency_code 固定为 gold；amount 是 JSON 数字，最多两位小数；reason 是中文说明。执行受益人固定为 appeal_adventurer，金额与补发资格仍须满足既有证据和额度规则。'
_APPEAL_PATCH_ALLOWED_FIELDS = _APPEAL_RECORD_SECTIONS | frozenset({'create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'team_pool', 'reclaim_items', 'unassigned_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS})
_CREATE_OWNERSHIP_SOURCES = frozenset({'member_update', 'narrative_event', 'recent_activity'})
_CREATE_OWNERSHIP_FIELDS = frozenset({'owner_adventurer_id', 'ownership_source', 'evidence_event_indexes', 'narrative_claim_kind'})
_NARRATIVE_CREATE_CLAIM_KINDS = {'create_items': 'permanent_item_acquisition', 'create_traits': 'stable_trait_formation', 'create_skills': 'explicit_permanent_learning'}

def _data_appeal_ai_failure(stage: str, public_reason: str, *, error: object=None, raw_content: object=None, attempts: list[dict] | None=None) -> dict:
    """Build an admin-diagnostic failure envelope without exposing provider config."""
    diagnostic = {'_ai_call_failed': True, 'failure_stage': stage, 'summary': f'一审未完成：{public_reason}', 'player_reject_reason': public_reason, 'internal_error': str(error or '').strip()}
    if raw_content is not None:
        diagnostic['raw_content'] = raw_content
    if attempts:
        diagnostic['attempts'] = attempts
    return diagnostic

def _runtime_metadata_bool(value: object) -> bool | None:
    if type(value) is bool:
        return value
    normalized = str(value or '').strip().lower()
    if normalized == 'true':
        return True
    if normalized == 'false':
        return False
    return None

def _primary_structure_failure_stage(error_code: str | None) -> str:
    if error_code == 'empty_content':
        return 'empty_response'
    if error_code == 'truncated_output':
        return 'truncated_response'
    if error_code in {'invalid_response_envelope', 'missing_choices', 'missing_message'}:
        return 'response_invalid'
    return 'json_parse'

def _build_primary_structure_repair_rule(failure_stage: str) -> str:
    rule = '只返回一个完整 JSON 对象；顶层必须包含 summary、player_reject_reason、core_request_satisfied、withdrawal_requested、request_checklist、focus_refs、lookup_queries、patch、warnings。request_checklist 必须有 1至8 条并保持玩家原文顺序；每条必须完整包含 label、category、is_core、status、resolution_basis，且 status 与稳定依据枚举一致；禁止输出 request_id；稳定 request_id 由后端在结构校验通过后按顺序分配，不得截断、遗漏或把可独立裁决的诉求合并。' + DATA_APPEAL_LOOKUP_STRUCTURE_RULE + DATA_APPEAL_PATCH_RECORD_SECTION_STRUCTURE_RULE + DATA_APPEAL_CURRENCY_ADJUSTMENT_STRUCTURE_RULE
    if failure_stage == 'truncated_response':
        return rule + '上一份正文因长度上限被截断，系统不会把半成品重新提供给你。请基于同一冻结输入独立重做一次完整裁决，并采用紧凑表达：只保留逐项结论、必要证据定位和可执行 patch，删除重复解释；不得通过遗漏任何独立诉求、必填字段或已经有证据支持的操作来缩短输出。'
    return rule + '保持冻结输入、逐项诉求、证据判断和已经表达的业务裁决不变；只修复输出结构。' + ('上一份正文为空，因此基于同一冻结输入重新完成原任务一次。' if failure_stage == 'empty_response' else '')
_APPEAL_CODENAME_ADDENDUM = '\n\n探险代号说明（陪审必读）：\n- 主探险 AI 生成剧情时使用【M1】/【M2】… 或 bare M1/M2 作为队员代号，代替玩家自定义姓名；历史日志也可能出现「冒险者甲/乙」或 #57 等称呼。\n- 成员身份按 `member_id_to_name` 与 `party_roster` 的结构化映射确定，其中的中性代号就是本轮有效称呼。`player_reject_reason`、`summary` 及其它输出沿用同一称呼。\n- 映射外称呼若无法唯一归属，保留原文并在 `warnings` 标注「无法映射的代号」；该不明身份不能作为对成员执行 create/update/delete 的依据。\n\n输入 JSON 省略规则（省 token）：\n- user 消息里的 JSON 会省略空字段/空容器；**未出现的键 = 空/无/不适用**，不要当作缺失数据或系统故障。\n- 任一可选键未出现时，按该键语义对应的空值处理；布尔状态只在 true 时下发的字段，缺失即 false。\n'
_EQUIPMENT_DESCRIPTION_INHERITANCE_EVIDENCE_RULES = '\n\n改名装备描述继承候选（仅在 equipment_description_inheritance_candidates 非空时启用）：\n- 该字段由后端按同一成员、同一次探险做精确结构映射：当前被选中 equipment/warehouse 的名称精确出现在 member_update.new_equipments，predecessor_candidates 的稳定原名精确出现在同一 member_update.consumed_equipments，旧完整描述来自出发冻结快照。它只证明存在待审候选，不直接证明每个旧物与新物是同一件。\n- 必须结合 all_events、settlement_description、当前 custom_desc/catalog_desc 和候选旧描述，独立判断新名物是否是旧物经历耗尽、损伤、充能变化、形态变化等状态更新后的同一件。玩家文字只用于提出诉求，不能代替这项语义判断。\n- 关系成立时，以对应 predecessor 的完整描述为底稿，保留未发生变化的构成、用途、能力和限制，只把剧情与结算已经确立的状态变化精准写入最终 custom_desc。状态变化使某项旧效果失效时，应明确其当前失效状态，同时保留物品其余仍成立的信息；不得把旧的有效状态原样恢复，也不得用一句状态摘要替换整份描述。\n- 当前名称与当前稀有度是独立结算结果；描述继承本身不要求还原旧名、旧稀有度、旧 type 或其它字段。无法确认继承关系时，不据此修改。\n'
_EQUIPMENT_RARITY_RESTORE_EVIDENCE_RULES = '\n\n装备原稀有度恢复（equipment_rarity_restore_candidates）：\n- 候选由后端从本人同次探险的最终通过回执生成，绑定 source_equipment_id、当前选中的 target_type/target_id、before_rarity 与 settled_rarity；完整状态、唯一对应关系及后续流水已核对。它只证明具体物品曾被降档且仍可定位，不直接证明降档错误。候选不授权删除、补发同名物或替换成别的记录。\n- 独立核对本次完整剧情、该物品权威描述和结算回执。如果它们确实不支持与降档相符的永久弱化，而回执发生无依据的降档，可将该 target_id 的 rarity 恢复为候选的 before_rarity。仅有可恢复的暂时状态变化而未改变物品固有能力，不能据此永久降档；正文已确立真实永久损失时，必须保留后果。玩家主张和当前图鉴档位不能创设旧档。\n- 这是恢复已持有物的原档，属于普通“其他问题”申诉的修复权限，不能仅因目标高于当前值而按越权驳回。恢复值必须恰为 before_rarity，只对该候选记录生效；无候选、候选无法与当前记录对应或语义证据不足时，不生成提档操作，按各自既有查证和裁决出口处理。缺少执行候选本身不能证明剧情中的降档合理；初审需要更多记录时，按既有 lookup_pending 合同提出实际所需的查证请求，二审另按其 repair_pending 出口判断可执行性。\n- 合法完整 patch 结构例（语义修复已独立成立）：候选 target_type=warehouse、target_id=17、before_rarity=legendary、settled_rarity=epic，输出 {"warehouse":{"update":[{"id":17,"rarity":"legendary"}],"delete":[]}}。恢复权限只覆盖该行 rarity，其它字段仍服从各自合同。\n- 非法完整 patch 例：候选只证明 target_id=17、before_rarity=epic、settled_rarity=rare，却输出 {"warehouse":{"update":[{"id":17,"rarity":"legendary"},{"id":18,"rarity":"epic"}],"delete":[]}}。第一项超过原档，第二项借用其它物品的证据，两项均无恢复权限。\n'
_SETTLEMENT_ASSET_MUTATION_EVIDENCE_RULES = '\n\n到账回执资产修改证据（强制）：\n- expedition_evidence.settlement_receipt.asset_mutations 仅在最终写回回执 status=passed 时提供，是后端在事务内重读权威表后生成的稳定 ID 修改记录，不是剧情 AI 自报。asset_type 只会是 trait / skill / equipment，operation 只会是 created / updated。\n- operation=updated 时，asset_name 是写回后的定位名称；before / after 只列本次实际改变的同一组白名单字段，分别表示该字段写入前后的精确值，空字符串、false、0、null 也是有效值。两个对象都未列出的字段表示本次未改变，不是证据缺失；出发快照只负责出发时状态与归属，不能替代这份结算变化。\n- operation=created 时没有旧记录，before 省略；after 是新记录写回后的白名单状态，空值或默认值可以省略。不得把 before 缺失误判为证据不完整。\n- 玩家争议本次探险是否新增或修改了某个特质、技能、装备，以及名称、描述、次数、稀有度或进化阶段如何变化时，优先使用同一 asset_id 的 operation 与稀疏 before / after；当前同名记录只能证明申诉时现状，不能替代历史变化值。\n- 旧探险回执可能没有 asset_mutations；字段缺失表示没有可用的精确修改审计，不能据此断言“没有修改”或编造前值。\n- asset_mutations 只提供证据与定位，不直接授权回滚、删除、扣除或补发。不得仅凭回执中的 asset_id 生成 patch target_id / source_id；任何修正仍须定位到系统提供的当前角色记录，并遵守既有 patch 白名单、破坏性动作和复核规则。\n'
_RECOVERY_AUTHORITY_EVIDENCE_RULES = '\n\n恢复字段统一证据边界（特质/技能/装备，强制）：\n- 玩家本次申诉理由、补充口述、建议文案、期望时长，以及陪审团既有 summary/reason/player_reason/reject_reason，都只用于定位诉求，不是恢复事实证据。不得单凭这些文字新增或补写 recovery_minutes、trigger_recovery、recovery_evidence_event_indexes、recovery_policy、recovery_stage、recovery_required 或 recovery_hours。\n- 至少一项服务端提供的权威全文必须与精确目标建立唯一关系，并明确写出具体恢复方式、条件、周期或时长：expedition_evidence.all_events 中的完整剧情正文，或 selected_records / related_records / all_player_records / 出发冻结快照中的技能 description、装备 custom_desc/catalog_desc、特质 description。只表达暂态不适或不可用、却未明确恢复机制，或与目标无法唯一绑定，不足以添加恢复字段。\n- 本轮陪审团根据玩家文字自行拟写或改写的 description/custom_desc，不得再作为同一 patch 添加恢复字段的证据；create 记录只能采用输入中已有的权威剧情、结算或冻结描述建立恢复字段。没有上述权威证据时保持现有恢复字段不变；新建记录按其既有专项契约写 0 或不写，不得猜测。\n'
_DATA_APPEAL_SYSTEM_PROMPT = ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_RECORD_SYSTEM_PROMPT = ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_COMMON_TAIL = ''
_DATA_APPEAL_COMMON_TAIL += ''
_DATA_APPEAL_DATA_FIX_SYSTEM_PROMPT = ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
_DATA_APPEAL_SYSTEM_PROMPT += ''
from plugins.adventurer_s1.services.s1_free_appeal_contract import FREE_APPEAL_COMMON, FREE_APPEAL_PRIMARY
_DATA_APPEAL_SYSTEM_PROMPT += FREE_APPEAL_PRIMARY
_DATA_APPEAL_DATA_FIX_SYSTEM_PROMPT += FREE_APPEAL_PRIMARY

def _validate_appeal_record_section(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    value = {key: value[key] for key in ('update', 'delete') if key in value}
    if frozenset(value) != frozenset({'update', 'delete'}):
        return None
    normalized: dict[str, list[dict]] = {'update': [], 'delete': []}
    for action in ('update', 'delete'):
        rows = value.get(action)
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                return None
            raw_id = row.get('id')
            record_id, _ = normalize_ascii_integer(raw_id, minimum=1)
            if type(record_id) is not int:
                return None
            if record_id <= 0:
                return None
            normalized[action].append({'id': record_id} if action == 'delete' else {**row, 'id': record_id})
    return normalized

def _project_create_ownership_fields(row: dict, *, section: str) -> dict:
    """Keep only a complete typed ownership claim; malformed claims become unsupported."""
    projected = dict(row)
    present = _CREATE_OWNERSHIP_FIELDS.intersection(projected)
    if not present:
        return projected
    owner_id, _ = normalize_ascii_integer(projected.get('owner_adventurer_id'), minimum=1)
    if type(owner_id) is not int or owner_id <= 0:
        owner_id = None
    source = str(projected.get('ownership_source') or '').strip()
    indexes = projected.get('evidence_event_indexes', [])
    normalized_indexes: list[int] = []
    valid_indexes = isinstance(indexes, list) and len(indexes) <= 16
    if valid_indexes:
        for index in indexes:
            normalized_index, _ = normalize_ascii_integer(index, minimum=0)
            if type(normalized_index) is not int or normalized_index < 0:
                valid_indexes = False
                break
            if normalized_index not in normalized_indexes:
                normalized_indexes.append(normalized_index)
    complete = owner_id is not None and source in _CREATE_OWNERSHIP_SOURCES and valid_indexes and (source != 'narrative_event' or bool(normalized_indexes)) and (source != 'narrative_event' or str(projected.get('narrative_claim_kind') or '').strip() == _NARRATIVE_CREATE_CLAIM_KINDS.get(section))
    if not complete:
        for key in _CREATE_OWNERSHIP_FIELDS:
            projected.pop(key, None)
        return projected
    projected['owner_adventurer_id'] = owner_id
    projected['ownership_source'] = source
    projected['evidence_event_indexes'] = normalized_indexes
    if source != 'narrative_event':
        projected.pop('narrative_claim_kind', None)
    else:
        projected['narrative_claim_kind'] = _NARRATIVE_CREATE_CLAIM_KINDS[section]
    return projected

def _validate_data_appeal_patch(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    value = {str(key): section_value for key, section_value in value.items() if str(key) in _APPEAL_PATCH_ALLOWED_FIELDS}
    normalized: dict = {}
    for section, section_value in value.items():
        if section == 'unassigned_items' and section_value is None:
            normalized[section] = []
        elif section in _APPEAL_RECORD_SECTIONS:
            validated = _validate_appeal_record_section(section_value)
            if validated is None:
                return None
            normalized[section] = validated
        elif section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            validated = project_valid_data_appeal_destructive_actions(section, section_value)
            if validated is None:
                normalized[section] = []
                continue
            normalized[section] = validated
        elif section in {'create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'reclaim_items', 'unassigned_items'}:
            if not isinstance(section_value, list) or any((not isinstance(row, dict) for row in section_value)):
                return None
            if section in {'create_items', 'create_traits', 'create_skills'}:
                normalized[section] = [_project_create_ownership_fields(row, section=section) for row in section_value]
            elif section == 'currency_adjustments':
                normalized[section] = [normalize_data_appeal_currency_adjustment(row)[0] for row in section_value]
            elif section == 'unassigned_items':
                normalized[section] = project_team_loot(section_value)
            else:
                normalized[section] = section_value
        elif section == 'team_pool':
            if section_value is not None and (not isinstance(section_value, dict)):
                return None
            normalized[section] = section_value
    return normalized

def _normalize_data_appeal_lookup_queries(value: object) -> tuple[list[dict], str | None]:
    """只归一查询协议的等价拼写，名称与实际查询范围仍按白名单校验。"""
    if not isinstance(value, list):
        return ([], 'result.lookup_queries.not_array')
    error = 'result.lookup_queries.too_many' if len(value) > 24 else None
    normalized: list[dict] = []
    for index, raw_row in enumerate(value[:24]):
        path = f'result.lookup_queries[{index}]'
        if not isinstance(raw_row, dict):
            error = error or f'{path}.not_object'
            continue
        row = {key: raw_row[key] for key in ('type', 'name') if key in raw_row}
        if not {'type', 'name'}.issubset(row):
            error = error or f'{path}.required_fields_missing'
            continue
        query_type = row['type']
        if isinstance(query_type, str):
            query_type = query_type.strip().lower()
            query_type = _APPEAL_LOOKUP_SECTION_ALIASES.get(query_type, query_type)
        else:
            query_type = None
        if query_type not in _APPEAL_RECORD_SECTIONS:
            error = error or f'{path}.type_invalid'
            continue
        name = row['name']
        if not isinstance(name, str) or not name.strip():
            error = error or f'{path}.name_invalid'
            continue
        normalized.append({'type': query_type, 'name': name.strip()})
    return (normalized, error)

def _validate_data_appeal_ai_result_with_status(result: object, *, diagnostics_out: dict | None=None) -> tuple[dict | None, bool]:
    """Normalize the AI envelope while keeping executable patch fields strict.

    普通展示/辅助字段仍会安全投影；request_checklist 已是二审授权范围，
    因此缺行、坏行或超限时直接失败关闭，不生成兜底或截断。request_id
    不由模型提供，而是在逐行校验通过后由后端按原顺序分配。函数同时返回
    完整契约状态，使缺失必填语义字段不会被记成成功调用。
    真正可执行的 patch 仍由 normalize_patch 校验，进入二审时还会由共享的
    严格 patch schema 再校验一次。
    """
    if diagnostics_out is not None:
        diagnostics_out.clear()
    if not isinstance(result, dict):
        return (None, False)
    required_envelope_fields = {'summary', 'player_reject_reason', 'core_request_satisfied', 'withdrawal_requested', 'request_checklist', 'focus_refs', 'lookup_queries', 'patch', 'warnings'}
    contract_valid = required_envelope_fields.issubset(result)
    summary = result.get('summary')
    player_reason = result.get('player_reject_reason', '')
    core_satisfied, _ = normalize_exact_boolean(result.get('core_request_satisfied'))
    withdrawal_requested, _ = normalize_exact_boolean(result.get('withdrawal_requested'))
    warnings = result.get('warnings', [])
    if not isinstance(player_reason, str):
        player_reason = ''
        contract_valid = False
    elif not player_reason.strip():
        contract_valid = False
    if not isinstance(summary, str) or not summary.strip():
        contract_valid = False
        if player_reason.strip():
            summary = f'AI 已完成分析：{player_reason.strip()}'
        else:
            return (None, False)
    if type(core_satisfied) is not bool:
        core_satisfied = False
        contract_valid = False
    if type(withdrawal_requested) is not bool:
        withdrawal_requested = False
        contract_valid = False
    if not isinstance(warnings, list):
        warnings = []
        contract_valid = False
    normalized_warnings = [item.strip() for item in warnings if isinstance(item, str) and item.strip()]
    if len(normalized_warnings) != len(warnings):
        contract_valid = False
    warnings = normalized_warnings
    checklist = result.get('request_checklist', [])
    if not isinstance(checklist, list) or not checklist or len(checklist) > 8:
        return (None, False)
    normalized_checklist: list[dict] = []
    for row_index, row in enumerate(checklist):
        if not isinstance(row, dict):
            return (None, False)
        row = {key: row[key] for key in ('label', 'category', 'is_core', 'status', 'resolution_basis', 'detail') if key in row}
        if not {'label', 'category', 'is_core', 'status', 'resolution_basis'}.issubset(row):
            return (None, False)
        label = row.get('label')
        detail = row.get('detail')
        if not isinstance(label, str) or not label.strip():
            return (None, False)
        if row.get('category') not in _APPEAL_CHECKLIST_CATEGORIES:
            return (None, False)
        if row.get('status') not in _APPEAL_CHECKLIST_STATUSES:
            return (None, False)
        resolution_basis = row.get('resolution_basis')
        if resolution_basis not in S1_DATA_APPEAL_RESOLUTION_BASES or resolution_basis not in _APPEAL_CHECKLIST_RESOLUTION_BASES_BY_STATUS[row['status']]:
            return (None, False)
        is_core, is_core_error = normalize_exact_boolean(row.get('is_core'))
        if is_core_error:
            return (None, False)
        if detail is not None and (not isinstance(detail, str)):
            detail = None
            contract_valid = False
        normalized_checklist.append({'request_id': f'request_{row_index + 1}', 'label': label.strip(), 'category': row['category'], 'is_core': is_core is True, 'status': row['status'], 'resolution_basis': resolution_basis, **({'detail': detail.strip()} if isinstance(detail, str) and detail.strip() else {})})
    core_rows = [row for row in normalized_checklist if row['is_core']]
    if not core_rows:
        return (None, False)
    derived_core_satisfied = all((row['status'] == 'done' for row in core_rows))
    if core_satisfied != derived_core_satisfied:
        contract_valid = False
    core_satisfied = derived_core_satisfied
    focus_refs = result.get('focus_refs', {})
    if not isinstance(focus_refs, dict):
        focus_refs = {}
        contract_valid = False
    if not set(_APPEAL_RECORD_SECTIONS).issubset(focus_refs) and set(focus_refs) - set(_APPEAL_RECORD_SECTIONS):
        contract_valid = False
    normalized_focus: dict[str, list[str]] = {}
    for section in _APPEAL_RECORD_SECTIONS:
        refs = focus_refs.get(section, [])
        if not isinstance(refs, list):
            refs = []
            contract_valid = False
        normalized_refs = [item.strip() for item in refs if isinstance(item, str) and item.strip()]
        if len(normalized_refs) != len(refs):
            contract_valid = False
        normalized_focus[section] = normalized_refs
    normalized_queries, lookup_error = _normalize_data_appeal_lookup_queries(result.get('lookup_queries', []))
    if lookup_error:
        contract_valid = False
        if diagnostics_out is not None:
            diagnostics_out['validation_error_code'] = lookup_error
    if any((row['resolution_basis'] == 'lookup_pending' for row in normalized_checklist)) and (not normalized_queries) and (not any(normalized_focus.values())):
        return (None, False)
    raw_patch = result.get('patch')
    if not isinstance(raw_patch, dict):
        contract_valid = False
    patch = {str(key): value for key, value in raw_patch.items() if str(key) in _APPEAL_PATCH_ALLOWED_FIELDS} if isinstance(raw_patch, dict) else {}
    normalized = {'summary': summary.strip(), 'player_reject_reason': player_reason.strip(), 'core_request_satisfied': core_satisfied, 'withdrawal_requested': withdrawal_requested, 'request_checklist': normalized_checklist, 'focus_refs': normalized_focus, 'lookup_queries': normalized_queries, 'patch': patch, 'warnings': warnings}
    return (normalized, contract_valid)

def _validate_data_appeal_ai_result(result: object) -> dict | None:
    normalized, _ = _validate_data_appeal_ai_result_with_status(result)
    return normalized

@with_player_name_aliases
def call_data_appeal_ai(appeal_id: int, player_note: str, snapshot_before: dict, appeal_adventurer: dict | None=None, expedition_evidence: dict | None=None, recent_activity: list | None=None, prior_appeal_grants: list | None=None, party_roster: list | None=None, team_pool_status: dict | None=None, appeal_fact_sheet: dict | None=None, appeal_category: str='other', token_usage_out: dict | None=None, runtime_metadata_out: dict[str, object] | None=None) -> dict | None:
    """调用 AI 生成 patch。返回解析后的 dict 或 None。

    一审不再注入全量 all_player_records；需核对其它条目时用 focus_refs / lookup_queries，
    由后端匹配后交给二审 related_records。
    """
    if runtime_metadata_out is not None:
        runtime_metadata_out['positive_tokens_consumed'] = False

    def _capture_positive_token_usage(usage: dict | None, *, request_metadata: dict | None=None, request_exception: BaseException | None=None) -> None:
        if runtime_metadata_out is not None and runtime_metadata_out.get('positive_tokens_consumed') is not True and s1_ai_usage_has_positive_tokens(usage, request_metadata=request_metadata, request_exception=request_exception):
            runtime_metadata_out['positive_tokens_consumed'] = True
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('data_appeal')
    except AIProfileAssignmentError as exc:
        logger.error('数据申诉 AI 用途路由不可用: %s', exc)
        return _data_appeal_ai_failure('configuration', '自动分析服务配置缺失，请稍后重试', error=str(exc))
    if not api_key or not api_base:
        logger.error('数据申诉 AI：MIMO API key 或 base URL 未配置')
        return _data_appeal_ai_failure('configuration', '自动分析服务配置缺失，请稍后重试', error='MIMO API key 或 base URL 未配置')
    codename_mapping: dict[str, str] = {}
    if expedition_evidence:
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
        expedition_evidence, codename_mapping = AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai(expedition_evidence)
        if codename_mapping:
            player_note = AdventurerS1DataAppealService.restore_codenames_in_appeal_narrative(player_note, codename_mapping)
    evidence_payload = expedition_evidence
    payload = {}
    split_evidence_payload = {}
    if appeal_fact_sheet:
        split_evidence_payload['appeal_fact_sheet'] = appeal_fact_sheet
    if party_roster:
        split_evidence_payload['party_roster'] = party_roster
    if evidence_payload:
        split_evidence_payload['expedition_evidence'] = evidence_payload
    if appeal_adventurer:
        payload['appeal_adventurer'] = appeal_adventurer
    payload['appeal_category'] = appeal_category
    payload['selected_records'] = snapshot_before
    if prior_appeal_grants:
        payload['prior_appeal_grants'] = prior_appeal_grants
    if recent_activity:
        payload['recent_activity'] = recent_activity
    if team_pool_status:
        pool_status = dict(team_pool_status)
        if pool_status.get('settled'):
            pool_status = {'settled': True, **{k: v for k, v in pool_status.items() if k != 'settled' and v}}
        else:
            pool_status = {k: v for k, v in pool_status.items() if k != 'settled' and v}
        if pool_status:
            payload['team_pool_status'] = pool_status
    payload['player_note'] = player_note
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    payload = AdventurerS1DataAppealService.prepare_appeal_ai_user_payload(payload)
    if split_evidence_payload:
        split_evidence_payload = AdventurerS1DataAppealService.prepare_appeal_ai_user_payload(split_evidence_payload)
    destructive_action_max = get_data_appeal_destructive_action_max()
    task_prompt = (_DATA_APPEAL_DATA_FIX_SYSTEM_PROMPT if appeal_category == 'data_fix' else _DATA_APPEAL_SYSTEM_PROMPT).replace('<DATA_APPEAL_DESTRUCTIVE_ACTION_MAX>', str(destructive_action_max))
    shared_system_prompt = FREE_APPEAL_COMMON + S1_DATA_APPEAL_PLAYER_DIRECTED_CONTENT_RULES + _APPEAL_CODENAME_ADDENDUM + S1_APPEAL_DESCRIPTION_INPUT_RULE + ''
    task_user_prompt = '【本次申诉输入】\n' + json.dumps(pack_appeal_description_inputs(payload), ensure_ascii=False, separators=(',', ':'))
    shared_evidence_prefix_enabled = has_app_context() and current_app.config.get(S1_DATA_APPEAL_SHARED_EVIDENCE_PREFIX_CONFIG, True) is True
    messages = build_s1_data_appeal_messages(shared_system_prompt=shared_system_prompt, task_system_prompt=task_prompt, shared_evidence_payload=split_evidence_payload, task_user_prompt=task_user_prompt, shared_evidence_prefix_enabled=shared_evidence_prefix_enabled)
    if shared_evidence_prefix_enabled and split_evidence_payload:
        log_s1_data_appeal_cache_diagnostics(logger, appeal_id=appeal_id, stage='first_review', shared_system_prompt=shared_system_prompt, shared_evidence_payload=split_evidence_payload, shared_evidence_content=messages[1]['content'])
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    adventurer_id = None
    if isinstance(appeal_adventurer, dict):
        raw_adv_id = appeal_adventurer.get('adventurer_id') or appeal_adventurer.get('id')
        if raw_adv_id is not None:
            adventurer_id = int(raw_adv_id)
    try:
        retry_limit = max(0, min(1, int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_STRUCTURE_RETRIES', 1))))
    except (TypeError, ValueError):
        retry_limit = 1
    request_messages = list(messages)
    attempts: list[dict] = []
    final_stage = 'response_invalid'
    final_validation_error = 'response.not_received'
    final_raw_excerpt = ''

    def validate_data_appeal_response(decoded) -> S1AiResponseContractResult:
        json_result = decoded.json_result
        if json_result.data is None:
            return S1AiResponseContractResult.reject(f"response.{json_result.error_code or 'invalid'}", failure_stage=_primary_structure_failure_stage(json_result.error_code))
        contract_diagnostics: dict = {}
        parsed_candidate, contract_valid = _validate_data_appeal_ai_result_with_status(json_result.data, diagnostics_out=contract_diagnostics)
        if parsed_candidate is None:
            return S1AiResponseContractResult.reject(contract_diagnostics.get('validation_error_code') or 'result.required_conclusion_missing')
        if not contract_valid:
            return S1AiResponseContractResult.reject(contract_diagnostics.get('validation_error_code') or 'result.envelope_contract_relaxed')
        return S1AiResponseContractResult.accept(parsed_candidate)
    for response_attempt in range(retry_limit + 1):
        attempt_number = response_attempt + 1
        request_metadata: dict[str, str] = {}
        response = None
        response_json = None
        usage = None
        runtime_model = model
        content = ''
        finish_reason = None
        try:
            db_manager.remove()
            response = post_s1_ai_request(f'{api_base}/chat/completions', feature='data_appeal', json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers=headers, json={'model': model, 'messages': request_messages, 'temperature': 0.2}, timeout=timeout, response_validator=validate_data_appeal_response)
            decoded = parse_s1_ai_http_json_response(response)
            response_json = decoded.http_result.response_json or {}
            json_result = decoded.json_result
            content = json_result.content
            if runtime_metadata_out is not None and isinstance(content, str) and content.strip():
                runtime_metadata_out['positive_tokens_consumed'] = True
            runtime_metadata = enrich_s1_ai_response_metadata(response, response_json)
            runtime_model = str(runtime_metadata.get('model') or request_metadata.get('model') or model)
            usage = decoded.http_result.usage
            _capture_positive_token_usage(usage, request_metadata=request_metadata)
            finish_reason = json_result.finish_reason
        except Exception as exc:
            _capture_positive_token_usage(usage, request_metadata=request_metadata, request_exception=exc)
            runtime_metadata = get_s1_ai_response_metadata(response) if response is not None else dict(request_metadata)
            runtime_model = str(runtime_metadata.get('model') or request_metadata.get('model') or model)
            response_body = getattr(response, 'text', None) if response is not None else None
            raw_excerpt = build_s1_ai_raw_excerpt(response_body or content)
            safe_error = str(exc).replace(api_base, '[申诉分析接口]').replace(model, '[申诉分析模型]')[:500]
            attempts.append({'attempt_number': attempt_number, 'failure_stage': 'http_or_response', 'validation_error_code': None, 'finish_reason': finish_reason, 'raw_content': raw_excerpt, 'runtime_metadata': {key: runtime_metadata[key] for key in ('profile_id', 'provider', 'model', 'thinking_mode', 'json_object_requested', 'json_object_mode', 'request_id') if runtime_metadata.get(key)}})
            record_token_usage(call_type='data_appeal_ai', usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, is_success=False, failure_stage='http_or_response', finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=response_attempt > 0, failed_response_excerpt=raw_excerpt, record_without_usage=True, request_metadata=request_metadata, request_exception=exc)
            if response is not None:
                log_s1_ai_response_outcome(response, feature='data_appeal', result_status='failed', finish_reason=finish_reason, repair_applied=response_attempt > 0, error_code='http_or_response', attempt_number=attempt_number)
            logger.warning('数据申诉 AI 调用失败 (appeal=%s): %s', appeal_id, safe_error)
            return _data_appeal_ai_failure('http_or_response', '自动分析服务暂时不可用，请稍后重试', error=safe_error, raw_content=raw_excerpt, attempts=attempts)
        parsed = None
        envelope_contract_valid = False
        validation_error_code = None
        if json_result.data is None:
            failure_stage = _primary_structure_failure_stage(json_result.error_code)
            validation_error_code = f"response.{json_result.error_code or 'invalid'}"
        else:
            contract_diagnostics: dict = {}
            parsed, envelope_contract_valid = _validate_data_appeal_ai_result_with_status(json_result.data, diagnostics_out=contract_diagnostics)
            if parsed is None:
                failure_stage = 'result_validation'
                validation_error_code = contract_diagnostics.get('validation_error_code') or 'result.required_conclusion_missing'
            elif not envelope_contract_valid:
                failure_stage = 'result_validation'
                validation_error_code = contract_diagnostics.get('validation_error_code') or 'result.envelope_contract_relaxed'
            else:
                failure_stage = None
        repair_applied = json_result.repair_applied or response_attempt > 0
        attempt_diagnostic = {'attempt_number': attempt_number, 'failure_stage': failure_stage, 'validation_error_code': validation_error_code, 'finish_reason': finish_reason, 'repair_applied': repair_applied, 'raw_content': json_result.raw_excerpt, 'runtime_metadata': {key: runtime_metadata[key] for key in ('profile_id', 'provider', 'model', 'thinking_mode', 'json_object_requested', 'json_object_mode', 'request_id') if runtime_metadata.get(key)}}
        attempts.append(attempt_diagnostic)
        if parsed is not None and envelope_contract_valid:
            if codename_mapping:
                from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
                AdventurerS1DataAppealService.restore_codenames_in_appeal_ai_response(parsed, codename_mapping)
            deferred_payload = {'usage': usage, 'model': runtime_model, 'profile_id': runtime_metadata.get('profile_id'), 'provider': runtime_metadata.get('provider'), 'appeal_id': appeal_id, 'adventurer_id': adventurer_id, 'attempt_number': attempt_number, 'envelope_contract_valid': envelope_contract_valid, 'failure_stage': failure_stage, 'validation_error_code': validation_error_code, 'finish_reason': finish_reason, 'json_object_mode': _runtime_metadata_bool(runtime_metadata.get('json_object_mode')), 'provider_request_id': runtime_metadata.get('request_id'), 'repair_applied': repair_applied, 'failed_response_excerpt': json_result.raw_excerpt}
            if token_usage_out is not None and usage:
                token_usage_out.clear()
                token_usage_out.update(deferred_payload)
            else:
                record_token_usage(call_type='data_appeal_ai', is_success=envelope_contract_valid, **{key: value for key, value in deferred_payload.items() if key != 'envelope_contract_valid'})
            log_s1_ai_response_outcome(response, feature='data_appeal', result_status='success' if envelope_contract_valid else 'normalized_output', finish_reason=finish_reason, repair_applied=repair_applied, error_code=validation_error_code, attempt_number=attempt_number)
            return parsed
        record_token_usage(call_type='data_appeal_ai', usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, is_success=False, failure_stage=failure_stage, validation_error_code=validation_error_code, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, failed_response_excerpt=json_result.raw_excerpt, record_without_usage=True, profile_id=runtime_metadata.get('profile_id'), provider=runtime_metadata.get('provider'), request_metadata=request_metadata)
        log_s1_ai_response_outcome(response, feature='data_appeal', result_status='invalid_output', finish_reason=finish_reason, repair_applied=repair_applied, error_code=validation_error_code, attempt_number=attempt_number)
        final_stage = failure_stage or 'result_validation'
        final_validation_error = validation_error_code
        final_raw_excerpt = json_result.raw_excerpt
        if response_attempt >= retry_limit:
            break
        repair_rule = _build_primary_structure_repair_rule(final_stage)
        request_messages = list(messages)
        if content and final_stage != 'truncated_response':
            request_messages.append({'role': 'assistant', 'content': content})
        request_messages.append({'role': 'user', 'content': f'上一份输出未通过结构校验。reason_code={final_validation_error or final_stage}。repair_rule={repair_rule}'})
    public_reason = {'empty_response': '自动分析没有返回内容，请稍后重试', 'truncated_response': '自动分析返回内容不完整，请稍后重试', 'result_validation': '自动分析未给出完整结论，请稍后重试'}.get(final_stage, '自动分析返回了无法解析的内容，请稍后重试')
    logger.warning('数据申诉 AI 结构修复耗尽 (appeal=%s stage=%s code=%s)', appeal_id, final_stage, final_validation_error)
    return _data_appeal_ai_failure(final_stage, public_reason, error=final_validation_error, raw_content=final_raw_excerpt, attempts=attempts)

def record_deferred_data_appeal_ai_usage(token_usage: dict | None, *, is_success: bool) -> None:
    """Record a parsed primary-appeal call after its executable contract is known."""
    if not isinstance(token_usage, dict) or not token_usage:
        return
    payload = dict(token_usage)
    token_usage.clear()
    usage = payload.get('usage')
    if not usage:
        return
    record_token_usage(call_type='data_appeal_ai', usage=usage, model=payload.get('model'), profile_id=payload.get('profile_id'), provider=payload.get('provider'), appeal_id=payload.get('appeal_id'), adventurer_id=payload.get('adventurer_id'), attempt_number=payload.get('attempt_number'), is_success=is_success, failure_stage=payload.get('consumer_failure_stage') or payload.get('failure_stage') or ('consumer_contract' if is_success is not True else None), validation_error_code=payload.get('consumer_validation_error_code') or payload.get('validation_error_code') or ('consumer.contract_failed' if is_success is not True else None), finish_reason=payload.get('finish_reason'), json_object_mode=payload.get('json_object_mode'), provider_request_id=payload.get('provider_request_id'), repair_applied=payload.get('repair_applied') is True, failed_response_excerpt=payload.get('failed_response_excerpt'))
_RARITY_REVIEW_SYSTEM_PROMPT = ''
_RARITY_REVIEW_SYSTEM_PROMPT += FREE_APPEAL_COMMON + '\n你只处理本次稀有度修改。按 player_note 要求为 items 中的精确 type/id 设置 rarity，\n使用 common|rare|epic|legendary；其它字段由普通申诉处理。不验证提档是否合理或有历史证据。\n未指定档位时按玩家要求的方向和物品描述选择，不要求证明历史差错。所有输入项各输出一次。\n只输出 {"items":[{"type":"equipment|warehouse","id":正整数,"rarity":"epic","reason":"中文修改说明"}]}。\n'

def _validate_rarity_review_result(result: object, source_items: list[dict]) -> dict | None:
    if not isinstance(result, dict):
        return None
    result = {key: result[key] for key in ('items',) if key in result}
    if frozenset(result) != frozenset({'items'}):
        return None
    rows = result.get('items')
    if not isinstance(rows, list) or len(rows) != len(source_items):
        return None
    expected: set[tuple[str, int]] = set()
    for source in source_items:
        if not isinstance(source, dict):
            return None
        source_type = source.get('type')
        raw_id = source.get('id')
        if source_type not in ('equipment', 'warehouse'):
            return None
        source_id, _ = normalize_ascii_integer(raw_id, minimum=1)
        if type(source_id) is not int:
            return None
        if source_id <= 0:
            return None
        expected.add((source_type, source_id))
    normalized: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        row = {key: row[key] for key in ('type', 'id', 'rarity', 'reason') if key in row}
        if frozenset(row) != frozenset({'type', 'id', 'rarity', 'reason'}):
            return None
        row_type = row.get('type')
        raw_id = row.get('id')
        row_id, _ = normalize_ascii_integer(raw_id, minimum=1)
        if type(row_id) is not int:
            return None
        key = (row_type, row_id)
        reason = row.get('reason')
        if key not in expected or key in seen or row.get('rarity') not in ('common', 'rare', 'epic', 'legendary') or (not isinstance(reason, str)) or (not reason.strip()) or (not _CHINESE_TEXT_RE.search(reason)):
            return None
        seen.add(key)
        normalized.append({'type': row_type, 'id': row_id, 'rarity': row['rarity'], 'reason': reason.strip()})
    return {'items': normalized} if seen == expected else None

@with_player_name_aliases
def call_rarity_review_ai(appeal_id: int, *, items: list[dict], expedition_evidence: dict, appeal_adventurer: dict | None=None, player_note: str='') -> dict | None:
    """副本掉落稀有度复核：新日志按全文，旧日志按所选段落判断 rarity。"""
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('data_appeal_rarity')
    except AIProfileAssignmentError as exc:
        logger.error('稀有度复核 AI 用途路由不可用: %s', exc)
        return None
    if not api_key or not api_base:
        logger.error('稀有度复核 AI：MIMO API key 或 base URL 未配置')
        return None
    evidence_block = {'expedition_log_id': expedition_evidence.get('expedition_log_id'), 'event_index': expedition_evidence.get('event_index'), 'auto_fallback': expedition_evidence.get('auto_fallback'), 'target_dungeon': expedition_evidence.get('target_dungeon'), 'party_name': expedition_evidence.get('party_name'), 'member_update': expedition_evidence.get('member_update'), 'party_member_updates_summary': expedition_evidence.get('party_member_updates_summary')}
    if expedition_evidence.get('auto_fallback'):
        evidence_block['all_events'] = expedition_evidence.get('all_events') or []
    else:
        evidence_block['selected_event_text'] = expedition_evidence.get('event_text')
    evidence_payload = {'expedition_evidence': evidence_block}
    payload = {'appeal_adventurer': appeal_adventurer or {}, 'items': items}
    payload['player_note'] = player_note
    user_prompt = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    evidence_prompt = json.dumps(evidence_payload, ensure_ascii=False, separators=(',', ':'))
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    usage = None
    runtime_model = model
    adventurer_id = None
    is_success = False
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None

    def validate_rarity_review_response(decoded) -> S1AiResponseContractResult:
        result = _validate_rarity_review_result(decoded.json_result.data, items)
        if result is None:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'result.rarity_review_contract_invalid')
        return S1AiResponseContractResult.accept(result)
    try:
        db_manager.remove()
        resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='data_appeal_rarity', request_metadata_out=request_metadata, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': '' + _RARITY_REVIEW_SYSTEM_PROMPT}, {'role': 'user', 'content': evidence_prompt}, {'role': 'user', 'content': '【本次复核输入】\n' + user_prompt}], 'temperature': 0.1}, timeout=timeout, response_validator=validate_rarity_review_response)
        decoded = parse_s1_ai_http_json_response(resp)
        usage = decoded.http_result.usage
        runtime_model = str(get_s1_ai_response_metadata(resp).get('model') or model)
        if isinstance(appeal_adventurer, dict):
            raw_adv_id = appeal_adventurer.get('adventurer_id') or appeal_adventurer.get('id')
            if raw_adv_id is not None:
                adventurer_id = int(raw_adv_id)
        result = _validate_rarity_review_result(decoded.json_result.data, items)
        is_success = result is not None
        return result
    except Exception as e:
        request_exception = e
        logger.warning('稀有度复核 AI 调用失败 (appeal=%s): %s', appeal_id, e)
        return None
    finally:
        if usage or request_exception is not None:
            record_token_usage(call_type='rarity_review_ai', usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=1, is_success=is_success, failure_stage=None if is_success else 'http_or_response', validation_error_code=None if request_exception is None else f'http_or_response.{type(request_exception).__name__}', record_without_usage=request_exception is not None, request_metadata=request_metadata, request_exception=request_exception)
