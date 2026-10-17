"""Bounded post-expedition entity preparation; no authority over settled outcomes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from plugins.adventurer_s1.services.s1_capability_profile_contract import (
    normalize_actor_capability_profile,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer
from plugins.adventurer_s1.services.s1_ai_prompt_shared import (
    S1_CORE_LEVEL_AND_ATTRIBUTE_REFERENCE_RULE,
    S1_NEW_CORE_STRENGTH_CALIBRATION_RULE,
)

ELIGIBLE_KINDS = frozenset({
    'local_anomaly', 'regional_incident', 'world_fact_catalyst',
    'extreme_crisis', 'unbeatable_horror',
})
ENTITY_TEXT_FIELDS = (
    'canonical_name', 'identity_summary', 'stable_principles',
    'long_term_goal', 'current_agenda', 'current_state', 'entry_title', 'entry_summary',
)
PREPARED_TEXT_FIELDS = (
    'canonical_name', 'identity_summary', 'stable_principles',
    'long_term_goal', 'current_agenda',
)

_PREPARATION_CHOICE_TASK = """你是深渊酒馆 S1 的特殊事件角色作者。本次探险已经结算，其结果保持既定。存活的既有实体沿原身份继续维护、学习与成长，本任务只补存尚未归档的存活主体，或为本场战败死亡的敌方准备以后可能相遇的独立强敌。
本轮交付至多一个有稳定身份、自主目标、行动判断与离场后持续性的实体。最终 events 已证实敌方在与队伍的战斗中战败死亡，且 allow_prepare=true 时优先 prepared；其余情况下，未归档的存活主体使用 observed。没有符合这两条路径的内容时 entity=null。
observed：allow_observed=true 时，优先归档最终 events 中已经与队伍交战、存活并能持续活动的未建档主体。canonical_name 沿用最终正文的稳定称谓。有有效 encounter_definition 对应的 individual 画像时，profile_index 精确引用该画像并继承其 subject 与能力；正文首次建立、没有对应有效画像的主体，profile_index=null，身份与本领仅从所引最终事件归纳，以 core_level_reference 为等级参照。已经归档的身份交回现有维护流程。没有有效冻结定义的旧画像仅作参照，不能借归档补造既成经历或本领。observed 保留真实身份与形态；prepared 的创作职责只约束本次新主体补充，不改写世界里其它实体的存在方式。"""

_PREPARATION_NEW_CHARACTER_TASK = """你是深渊酒馆 S1 的特殊事件角色作者。先核对最终 events 是否已证实敌方在与队伍的战斗中战败死亡；该事实成立且 allow_prepare=true 时，才按 prepared 创作一个尚未与队伍相遇的独立强敌，否则 entity=null。存活实体沿原身份继续维护、学习与成长。
新角色交付它在世界中生活至今的身份、欲求、行动意图及自身本领。它的完整实力足以战胜本场冻结能力所代表的队伍实力，并承担本次红色或黑色危险级别。
party 是来源正文实际使用的出发能力与状态，events 是已经结束的实战，用于理解这些本领如何参与交互；本轮的主角是你在当地未定义空间新建立的生命。世界资料用于核对相容性，已结算结果保持既定。没有相容内容时 entity=null。"""

_PREPARATION_BODY = """prepared：profile_index=null，本次首次建立档案的独立生命，可以早已在世界中生活；首次进入故事不等于刚刚诞生。创作从副本的世界来源与相容空白展开独立生命，局部已有生命和关系不代表副本全部。identity_summary 确立其生命身份、个人经历与自主欲求，current_agenda 写接下来想做什么，再由其存在方式、成长、修习和实际资源建立本领。这里交付角色档案与行动意图；行动实际发生、接触对象及对现场的影响由以后出场时的剧情建立，已结算现场继承原状。主体对自身欲求作出有立场的判断，能够与其它生命发生有意图的互动。超常形态同样由具体存在、自主判断与持续生活成立。
prepared 的输入分工：地点、世界线与 maintenance_plan 用于核对相容性；未定义空间允许创作角色自己的来历，有输入依据的诞生、成长和事件影响同样有效。结合角色自身经历与最终 events 的真实攻防、防护、承受、控制、脱离和协作，以 core_level_reference.basis_level 为共同参照设计本领，区分实际表现与人物猜测；人物最终生还只说明本次结果。evidence_event_indexes 引用实力校准的实际表现，不承担新角色曾经存在或诞生的证明。它没有参加已结算探险。涉及已发生的地点、人物或公共事件的断言沿用输入事实；角色知识来自自己的经历与实际传播。
observed_location_scope=unresolved 时，observed 只归档已实际出场的主体身份、本领和最后已知的自身处境；entry_summary 保持该次未绘局部的历史归属，不建立公共地点、地点资源、可通行路线或全地图现状。后续相遇沿主体自身可达范围与实际经历成立。allow_observed=false 时不归档来源主体；prepared 仍可独立成立在地图的未定义空间，来源事件也不因此成为全地图公共现状。
prepared 按【深渊酒馆·新核心实力定义】建立 capability_profile.capabilities，以 party 的冻结完整能力及最终 events 已证实的有效表现共同校准，形成明显足以战胜这支来源队伍的整体攻防与持续作战实力。红色正常对抗有实质致死风险，unbeatable_horror 整体压倒该队伍、不可战胜，常态完整接触足以轻易造成多人死亡。角色自身目标与所处冲突使其具备成为敌对目标的实际动机，具体相遇与结果由以后探险形成；后来的队伍成长和新表现仍按当时事实判断。
实体认知与能力分别成立。创作者掌握队伍表现不等于敌人认识这支队伍；本轮不创建反制知识或传播关系，也不改写已有实体实力。
maintenance_plan 是本批普通世界维护拟应用的变化，提案同时兼容当前事实和这些变化；后端会确认实际应用。完整角色核心（身份、行动、处境、能力画像及必要标记）须能装入 contract_limits.actor_core_chars，总量与单条长度均保持完整，不靠截去能力实现。
等级、能力档位与属性尺度沿共用【深渊酒馆·核心基础等级与属性尺度】执行。
输出纯 JSON：{"entity":null} 或 {"entity":{...}}。非空实体字段：
__ORIGIN_FIELDS__
defeated_enemy：prepared 必填对象，subject 为本场战败死亡敌方在最终 events 中的准确称谓，evidence_event_indexes 为证明其敌对战斗与战败死亡的非空去重事件编号数组；它与新角色是两个身份。observed 固定为 null。此证据只授权本次新角色准备，不代替既有实体的状态维护。
evidence_event_indexes 为非空去重的最终 events 零基下标数组，__EVIDENCE_BINDING__；
canonical_name、identity_summary、stable_principles、long_term_goal、current_agenda 均为非空字符串，长度上限见 contract_limits。
__OBSERVED_STATE_FIELDS__
这些文本字段各自使用一个 JSON 字符串承载完整内容，结构占位：{"stable_principles":"<主体持续遵循的原则>"}。
capability_profile 完整包含 basis_level、typical_profile、current_profile、condition、number_pressure、capabilities、limitations。
两组 profile 都恰含 force/mobility/cognition/durability/perception，值为 very_low/low/baseline/high/very_high/exceptional；
condition 为 impaired/normal/enhanced；number_pressure 为 none/low/medium/high/overwhelming；
capabilities 是非空字符串数组，limitations 是字符串数组，无固有限制可为空。每条边界完整表达，不写未来成员动作或结局。
其余身份、状态、范围、世界线关联与版本由后端签发；world_actor_name_registry 是有界命名提示，未列出不表示名称未被占用，后端写入时核对完整历史身份；它只用于避免重复身份，不授予任何既有角色更新权。"""

_SOURCE_REFERENCE_RULE = """party_ability_status=complete 表示 party 中的资产与说明沿来源正文的实际冻结版本，含当时生效的压缩描述与筛选结果；它描述出发实力，outcomes 和 events 另外描述已结算结果。未在上一场发动的已持有能力仍是实力参照，既有作用条件继续有效。创作者掌握这些资料不授予新实体对应知识；来源成员后来的变化不回填到本场。party_ability_status=unavailable 时只支持已有证据范围内的 observed 归档，allow_prepare=false。
events 每条含 event_index 与完整 text；event_index 是最终正文的明确零基编号。evidence_event_indexes 直接引用这些编号，相同文字的不同事件仍按各自编号区分。
归纳已出场主体的本领与限制时，核对整场相关事件，区分每次用法、作用范围和成立条件。单次局部结果不建立固有上限，后一次较弱效果不抹去此前已成立的更强作用；较强表现同样不代表所有用法都能取得该效果。写入的能力边界须容纳这些已发生的不同表现。
condition 表示主体相对自身常态的当前状态：impaired/normal/enhanced 分别为受损/常态/增幅，实力强大本身不等于增强状态。number_pressure 只表示参战数量带来的压力，单体没有数量优势时为 none；单体的作用范围、直接杀伤和持续行动由本领及五轴档位表达。"""


def build_preparation_rule(*, allow_observed: bool) -> str:
    """Give a prepared-only author one task; keep the dual path when authorized."""
    task = _PREPARATION_CHOICE_TASK if allow_observed else _PREPARATION_NEW_CHARACTER_TASK
    origin = ('origin 为 observed 或 prepared；profile_index 按上述身份分支；'
              if allow_observed else 'origin 固定为 prepared；profile_index 固定为 null；')
    evidence = ('observed 绑定出场与存续依据，prepared 绑定实力校准的实际表现'
                if allow_observed else '绑定实力校准的真实队伍表现')
    return (task + '\n' + S1_CORE_LEVEL_AND_ATTRIBUTE_REFERENCE_RULE
            + '\n' + S1_NEW_CORE_STRENGTH_CALIBRATION_RULE
            + '\n' + _SOURCE_REFERENCE_RULE + '\n' + _PREPARATION_BODY.replace(
        '__ORIGIN_FIELDS__', origin,
    ).replace('__EVIDENCE_BINDING__', evidence).replace(
        '__OBSERVED_STATE_FIELDS__',
        ('observed 另外输出 current_state、entry_title、entry_summary 三个非空字符串，'
         '分别保存最终事件中的自身状态、出场条目标题和已发生的相遇事实；prepared 只交付上述角色档案。')
        if allow_observed else '',
    ))


PREPARATION_RULE = build_preparation_rule(allow_observed=True)


REVIEW_RULE = """你是深渊酒馆 S1 的后续实体准备复核者，只核对这一份实体提案。不能改写探险正文、生命结果或候选。
observed 的 evidence_event_indexes 须支持同一主体实际出场与存续，稳定身份、能力基线与变化由最终 events 和有效 encounter_definition 绑定的准确画像支持。profile_index=null 表示正文首次建立且没有对应有效画像的主体，逐项核对其身份、本领和最后状态均由所引最终事件支持，不能借归档新增能力；等级采用 core_level_reference。已有有效画像时沿其准确身份继承，既有能力仅按真实变化更新，未来计划不当作完成的成长。observed_location_scope=unresolved 时仅授权主体自身归档，条目保留未绘局部的历史归属，不把该局部事实升级为公共地点、资源或通行条件。已死亡或已终结主体不建立为 active，名称登记中的旧主体不新建。world_actor_name_registry 是有界提示，完整历史身份由后端写入时核对；名单未包含某个名字不证明它是新身份。
prepared 在 allow_prepare=true 时创作未定义空间内首次建档的独立生命；首次建档不等于刚刚诞生。核对其生命身份、个人经历、自主欲求、行动意图和本领是否自洽，是否与地点、世界连续性及本批计划相容。current_agenda 是待实施意图，能力是它具备的本领，均不表示已改变现场。后端用规范名和身份简介建立入场条目，初始 current_state 为空，实际相遇与现场变化由以后剧情建立；这不构成缺少角色当前态的拒绝理由。角色可以拥有输入尚未定义的个人来历，有输入依据的诞生、成长和事件影响同样有效。地点材料约束相容性。observed 保留真实身份与形态、最终事件已成立的状态及出场条目。
prepared 的 evidence_event_indexes 只需指向用于实力校准的真实队伍表现，不要求 events 已记载这个新角色的存在、诞生或出场。概括和角色猜测不代替实战事实。提案若对已发生的地点、人物或公共事件作出断言，该部分仍须有输入依据；未定义的个人来历不授予补写上一场战斗、修改地点现状、势力关系或其它实体的权限。来源探险也不自动成为该角色已经感知或调查过的经历。
prepared 的 defeated_enemy 必须由所引最终事件证实为本场与队伍交战且战败死亡的敌方；存活、去向不明或仅失去某项作用都不成立。核对原主体与新角色的身份独立性。证据不成立时拒绝提案；该结论不授权修改原主体状态。
prepared 按共用【深渊酒馆·新核心实力定义】核对候选的完整本领，依据 party 的冻结完整能力及 events 已证实的有效表现：整体攻防与持续作战实力明显足以战胜这支来源队伍，且自身目标与当前冲突支持其作为敌对目标。红色须有实质致死风险，黑色须整体压倒该队伍且不可战胜；只具备支援或自保而缺少对应对抗本领时不满足。有因果依据的有效交互和固有限制照实保留。
提案须同时兼容 world_context、location_snapshot 与本批 maintenance_plan，后端会确认实际应用。等级与属性尺度沿共用规则，角色完整核心遵守 contract_limits.actor_core_chars，保持能力含义完整。
判断的是相遇前的身份、实力与连续性，不预测未来伤亡、不要求无法逃跑或所有玩家能力失效，也不按形态字词分类拒绝。
observed 的身份关联另用 source_profile_index 表达：同一主体在最终正文与画像中的称谓可以不同，核对身份和所引事件后，返回对应 encounter_profiles 的 profile_index；确实没有对应画像时返回 null。提案已引用 profile_index 时沿用同一编号。旧画像仍可用于确认主体身份，这个关联不把无有效 encounter_definition 的旧能力提升为权威，也不允许合并不同主体。该字段只将已完成的首战学习关联到原主体，不改写提案。
输出纯 JSON：{"approved":布尔值,"reason":"简短说明核对结果或具体冲突","source_profile_index":零基整数或null}。approved=true 表示完整提案满足所选 origin 的全部适用条件：prepared 包括身份、连续性和上述强度条件，observed 沿用真实出场、存续与能力继承条件。任一适用条件不成立时 approved=false，reason 说明具体缺项或冲突。prepared 或拒绝提案时 source_profile_index=null。批准或拒绝完整提案，不输出改写。"""


REVIEW_RULE += ('\n' + S1_CORE_LEVEL_AND_ATTRIBUTE_REFERENCE_RULE
                + '\n' + S1_NEW_CORE_STRENGTH_CALIBRATION_RULE
                + '\n' + _SOURCE_REFERENCE_RULE)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def normalize_proposal(raw, snapshot: dict) -> dict:
    if not isinstance(raw, dict) or 'entity' not in raw:
        raise ValueError('encounter_preparation.entity_missing')
    value = raw['entity']
    if value is None:
        return {'entity': None}
    if not isinstance(value, dict):
        raise ValueError('encounter_preparation.entity_invalid')
    origin = value.get('origin')
    if not isinstance(origin, str) or origin not in {'observed', 'prepared'}:
        raise ValueError('encounter_preparation.origin_invalid')
    if 'profile_index' not in value:
        raise ValueError('encounter_preparation.profile_index_missing')
    index = value['profile_index']
    profile = None
    if origin == 'observed':
        if snapshot.get('allow_observed') is not True:
            raise ValueError('encounter_preparation.observed_scope_unresolved')
        if index is not None:
            if not snapshot.get('encounter_definition'):
                raise ValueError('encounter_preparation.definition_missing')
            index, _ = normalize_ascii_integer(index, minimum=0)
            profiles = snapshot['encounter_profiles']
            # Accept an exact frozen subject reference only when it identifies
            # one row and agrees with the independently declared identity.
            if isinstance(index, str) and index == value.get('canonical_name'):
                matches = [i for i, row in enumerate(profiles)
                           if isinstance(row, dict) and row.get('subject') == index]
                if len(matches) == 1:
                    index = matches[0]
            if type(index) is not int or not 0 <= index < len(profiles):
                raise ValueError('encounter_preparation.profile_index_invalid')
            profile = profiles[index]
            if not isinstance(profile, dict) or profile.get('scope') != 'individual':
                raise ValueError('encounter_preparation.not_individual')
    elif index is not None or snapshot['allow_prepare'] is not True:
        raise ValueError('encounter_preparation.creation_not_authorized')
    limits = snapshot['contract_limits']
    result = {'origin': origin, 'profile_index': index}
    text_fields = PREPARED_TEXT_FIELDS if origin == 'prepared' else ENTITY_TEXT_FIELDS
    for field in text_fields:
        text = value.get(field)
        maximum = limits['name_chars'] if field in {'canonical_name', 'entry_title'} else limits['text_chars']
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > maximum:
            raise ValueError('encounter_preparation.text_invalid:' + field)
        result[field] = text.strip()
    if origin == 'prepared':
        # Admission creates identity and a selectable carrier, not a new event.
        # The same reviewed identity supplies the entry; no parallel scene draft.
        result.update(current_state='', entry_title=result['canonical_name'],
                      entry_summary=result['identity_summary'])
    if result['canonical_name'] in snapshot['world_actor_name_registry']:
        raise ValueError('encounter_preparation.identity_already_registered')
    if profile is not None and result['canonical_name'] != profile.get('subject'):
        raise ValueError('encounter_preparation.identity_mismatch')
    if origin == 'observed' and index is None and snapshot.get('encounter_definition'):
        if any(isinstance(row, dict) and row.get('subject') == result['canonical_name']
               for row in snapshot['encounter_profiles']):
            raise ValueError('encounter_preparation.frozen_profile_binding_required')
    evidence = value.get('evidence_event_indexes')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('encounter_preparation.evidence_missing')
    indexes = [normalize_ascii_integer(i, minimum=0)[0] for i in evidence]
    if (any(type(i) is not int or not 0 <= i < len(snapshot['events']) for i in indexes)
            or len(set(indexes)) != len(indexes)):
        raise ValueError('encounter_preparation.evidence_invalid')
    result['evidence_event_indexes'] = indexes
    if origin == 'prepared':
        defeated = value.get('defeated_enemy')
        if not isinstance(defeated, dict):
            raise ValueError('encounter_preparation.defeated_enemy_missing')
        subject = defeated.get('subject')
        if (not isinstance(subject, str) or not subject.strip()
                or len(subject.strip()) > limits['name_chars']
                or subject.strip() == result['canonical_name']):
            raise ValueError('encounter_preparation.defeated_enemy_identity_invalid')
        evidence = defeated.get('evidence_event_indexes')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('encounter_preparation.defeat_evidence_missing')
        indexes = [normalize_ascii_integer(i, minimum=0)[0] for i in evidence]
        if (any(type(i) is not int or not 0 <= i < len(snapshot['events']) for i in indexes)
                or len(set(indexes)) != len(indexes)):
            raise ValueError('encounter_preparation.defeat_evidence_invalid')
        result['defeated_enemy'] = {'subject': subject.strip(), 'evidence_event_indexes': indexes}
    else:
        result['defeated_enemy'] = None
    try:
        capability = normalize_actor_capability_profile(value.get('capability_profile'))
    except (TypeError, AttributeError):
        capability = None
    if capability is None or not capability['capabilities']:
        raise ValueError('encounter_preparation.capability_invalid')
    expected_level = (profile.get('basis_level') if profile is not None
                      else snapshot['core_level_reference']['basis_level'])
    if capability['basis_level'] != expected_level:
        raise ValueError('encounter_preparation.basis_level_mismatch')
    if any(len(item) > limits['capability_chars'] for field in ('capabilities', 'limitations')
           for item in capability[field]):
        raise ValueError('encounter_preparation.capability_too_long')
    result['capability_profile'] = deepcopy(capability)
    from types import SimpleNamespace
    from plugins.adventurer_s1.services.s1_world_actor_service import _actor_prompt_core
    # Reserve the longest IDs representable by the existing Integer columns.
    actor = SimpleNamespace(**result, id=2147483647, status='active', capability_revision=1)
    link = SimpleNamespace(actor_id=actor.id, world_story_thread_id=2147483647, lease_required=True)
    core = _actor_prompt_core(actor, {link.world_story_thread_id}, [link])[0]
    if len('\n'.join(core)) + 2 > limits['actor_core_chars']:
        raise ValueError('encounter_preparation.actor_core_too_long')
    return {'entity': result}


def normalize_review(raw, *, snapshot=None, proposal=None) -> dict:
    if (not isinstance(raw, dict) or type(raw.get('approved')) is not bool
            or not isinstance(raw.get('reason'), str) or not raw['reason'].strip()):
        raise ValueError('encounter_preparation.review_invalid')
    result = {'approved': raw['approved'], 'reason': raw['reason'].strip()}
    entity = (proposal or {}).get('entity') or {}
    if not raw['approved'] or entity.get('origin') != 'observed' or snapshot is None:
        return result
    profiles = snapshot.get('encounter_profiles') or []
    declared = entity.get('profile_index')
    available = [index for index, row in enumerate(profiles)
                 if isinstance(row, dict) and row.get('scope') == 'individual'
                 and not row.get('world_actor_id')]
    if not available:
        return {**result, 'source_profile_index': None}
    # Approval of an explicit proposal reference already covers its identity.
    # An unbound display name requires an explicit decision from the reviewer.
    if 'source_profile_index' not in raw and declared is None:
        raise ValueError('encounter_preparation.source_profile_index_missing')
    index = raw.get('source_profile_index', declared)
    index = normalize_ascii_integer(index, minimum=0)[0] if index is not None else None
    if ((index is not None and (type(index) is not int or index not in available))
            or (declared is not None and index != declared)):
        raise ValueError('encounter_preparation.source_profile_index_invalid')
    return {**result, 'source_profile_index': index}
