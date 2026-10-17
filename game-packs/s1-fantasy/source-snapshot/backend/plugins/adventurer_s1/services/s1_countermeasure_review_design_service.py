"""Reviewed mechanisms own facts and sources; design owns only adaptation."""
from copy import deepcopy

REVIEW_FORMAT = 'mechanism_review_v1'

REVIEW_SYSTEM_PROMPT = """你为 actors 中的长期主体整理它们从本场冒险者身上学到了什么。学习方向固定为：source_members 中的冒险者施用手段，actors 中的主体感知或承受该手段，并形成可保存的认识。observer_world_actor_id 绑定获得认识的主体；source_refs 绑定施用手段的冒险者。final_events 是已发生的遭遇，existing_knowledge 是主体原有认识，source_catalog 只帮助绑定冒险者来源。

先从整场事件找出冒险者实际施用的手段，再逐个判断哪个学习主体实际感知或承受了它。只有这两个条件同时成立才进入学习记录；主体自身的招式只作为理解交互的背景，不是待归档的冒险者手段，也不因无法绑定冒险者而进入 deferred。各个已成立的学习事项独立处理，尚不能归因的事项不影响其余事项。同一作用的出现、维持、变化与结束合成一项，独立机制分别记录；共同成立一个作用的来源合写。单次成败不代表固定上限。没有新增认识或可更新反馈的旧内容不重复建项；完整遭遇中没有任何符合条件的事项时 records=[]。

后台来源事实与主体知情分开。encountered_method、observed_effect 为后台保留对方实际施用的独立作用及其真实效果。recognized_method 是主体已可辨认的手段特征，用简短客观短语概括机制及必要条件；learning_feedback 只写主体由实际感知、直接承受或已建立的代理联系取得的反馈，包含确实完成的自身准备。对方私下操作、隐藏原理、远端结果与内心不因写在正文里就成为主体知识。unresolved_reason 简述尚未查明且影响应对的条件，无则为空。长期识别保留必要条件，历史地点、方向和动作顺序在不改变机制时留在后台事实中。主体固有结构与弱点由角色档案保存，学习聚焦遇到的对方手段。

matched_knowledge_id 只匹配同主体实际拥有的完整独立作用机制及效应类型，且有效边界连续；本次反馈或成败变化不单独改变身份，名称和施用者变化也不改变机制。局部相似不能匹配整个旧复合项，否则填 null。同主体同旧 ID 本次只输出一份完整更新，recognized_method 整合仍有效的已有认识与本次反馈。

source_refs 完整绑定共同成立该作用的冒险者来源，每项含 adventurer_id、source_kind、source_name、source_asset_id。source_kind 取 equipment、skill、trait、state、event；按 source_catalog 精确复制同成员同类型的资产身份。已给资产 ID 时使用该 ID，未给 ID 或非资产的实际事件、状态才用 null；目录本身不是施用证明。来源主体、施用与机制不能唯一确定时，不提交该项，在 deferred 中保留 observer_world_actor_id 和 reason，其余独立项继续。主体自身施用的能力不归给冒险者。reason 简述完整机制归因和来源绑定依据。

只输出 JSON 对象 records 与 deferred 数组。records 每项完整包含 observer_world_actor_id、matched_knowledge_id、encountered_method、observed_effect、unresolved_reason、recognized_method、learning_feedback、source_refs、reason。主体 ID 原样复制 actors 的整数 id；旧 ID 为已知整数或 null；上述文本字段均为字符串，除 unresolved_reason 可空外均非空。单项上述文本（不含 reason）与换行的合计不超过 memory_max_chars，简要保留必要信息即可。normal_targets.records 与 normal_targets.sources 为正常目标，合法独立机制或共同来源超过时完整保留。审核到事实、可知问题与来源结束，应对方法由下一职责设计。
"""

DESIGN_SYSTEM_PROMPT = """你负责设计长期主体在本次遭遇后的能力成长，并更新确实改变的既有战术。actors 是成长前完整档案，problems 是经核对且主体已取得的认识与反馈。每项 observer_world_actor_id 绑定获得认识的主体，recognized_method 描述冒险者施用的手段，learning_feedback 保留该主体对实际交互的观察或承受。先分清手段由谁施用、实际作用于谁、谁发生变化，再为获得认识的主体设计应对；其自身受创不构成另一方已有伤势或弱点的证据。对每个拟成长主体，以它的当前目标为准，从归属它的 problems 选出直接造成目标失败的一个决定性能力缺口；没有目标失败时，选择一个实际受阻的能力缺口。先在 review_indexes 绑定只支撑这个缺口的反馈，再为它创造已经掌握的新本领。本轮成长集中解决所选问题，其余观察与认识完整保留。原本领能处理的事项保留有效旧应对，有真实改变才更新。低认知主体也可按自身生命与感知方式适应，无需理解完整原理；隐秘信息与尚未完成的外部准备保持原状。

本主体的既有个人知识默认保留 existing_knowledge 的 knowledge_level 与 countermeasure_direction；新认识和引用势力知识形成的本次个人观察默认 observed 和空方向。势力知识供理解参考，其中原观察者的准备不成为本主体已经完成的准备。只交付这些默认值之外的真实变化，不重复整理整份清单。已理解机制可更新为 understood，无可行战术时方向可为空；有输入证明本主体已经完成的准备才用 prepared，方向非空。observed 的方向为空。新功能及依赖它的作用只在成长画像中设计；countermeasure_direction 只用成长前已有本领，写明怎样干预已知作用的成立环节；仅有应对意图不产生能力。每项 direction_max_chars 是方向的容量上限，不是目标篇幅。未改变的知识、完整事实、身份与来源由后台继承。
"""

from .s1_countermeasure_growth_service import GROWTH_OUTPUT_RULE
DESIGN_SYSTEM_PROMPT += '\n' + GROWTH_OUTPUT_RULE
DESIGN_SYSTEM_PROMPT += """

只输出 capability_growth 与 adaptations 两个数组。capability_growth 先按上述完整合同交付，无成长时为空。adaptations 只包含需要改变默认知识状态的项，每项含准确 review_index、knowledge_level、countermeasure_direction，同一索引至多一项。省略的项继承默认值，不丢弃已核对认识；没有知识变化时 adaptations=[]。
"""


def build_review_view(learning_input):
    from . import s1_countermeasure_learning_ai_service as L
    view = L._learning_ai_view(learning_input)
    view['source_catalog'] = [{k: row[k] for k in (
        'adventurer_id', 'source_kind', 'source_name', 'source_asset_id',
    )} for row in learning_input['source_catalog']]
    return view


def normalize_review(value, learning_input):
    from . import s1_countermeasure_learning_ai_service as L
    if not isinstance(value, dict) or not isinstance(value.get('records'), list) or not isinstance(value.get('deferred'), list):
        raise ValueError('learning_review_arrays_required')
    candidates, rejected = [], []
    # Preserve positions for the existing row-level validator and source binder.
    for raw in value['records']:
        row = {k: raw.get(k) for k in (
            'observer_world_actor_id', 'matched_knowledge_id', *L._EXPERIENCE_FACT_FIELDS,
        )} if isinstance(raw, dict) else {}
        row.update(learned_boundary=raw.get('recognized_method') if isinstance(raw, dict) else None,
                   knowledge_level='observed', countermeasure_direction='')
        candidates.append(row)
    observations, errors = L.normalize_experiences({'experiences': candidates}, learning_input)
    rejected.extend(errors)
    valid = []
    for observation in observations:
        index = observation['experience_index']
        raw = value['records'][index]
        feedback = raw.get('learning_feedback')
        texts = [raw.get(k) for k in (*L._EXPERIENCE_FACT_FIELDS, 'recognized_method', 'learning_feedback')]
        if not isinstance(feedback, str) or not feedback.strip():
            rejected.append({'review_index': index, 'reason': 'learning_review_feedback_required'})
        elif len('\n'.join(t.strip() for t in texts if t)) > learning_input['memory_max_chars']:
            rejected.append({'review_index': index, 'reason': 'learning_review_memory_invalid'})
        else:
            valid.append(observation)
    archive = {'records': [{
        'experience_index': row['experience_index'],
        'source_refs': value['records'][row['experience_index']].get('source_refs'),
        'reason': value['records'][row['experience_index']].get('reason'),
    } for row in valid], 'deferred': []}
    repairs = []
    bound, errors = L.seal_learning_archive(archive, valid, learning_input, source_repairs_out=repairs)
    rejected.extend(errors)
    # Source binder preserves list order but may isolate invalid siblings.
    accepted = []
    failed = {e['experience_index'] for e in errors if 'experience_index' in e}
    successful = [r for r in valid if r['experience_index'] not in failed]
    if len(successful) != len(bound):
        raise ValueError('learning_review_source_binding_mismatch')
    for observation, sources in zip(successful, bound):
        index = observation['experience_index']; raw = value['records'][index]
        accepted.append({**observation, 'review_index': index,
                         'learning_feedback': raw['learning_feedback'].strip(),
                         'source_refs': sources['source_refs'], 'reason': raw['reason'].strip(),
                         'source_identity_repairs': [r for r in repairs if r['experience_index'] == index]})
    ids = {a['id'] for a in learning_input['actors']}
    for raw in value['deferred']:
        if (isinstance(raw, dict) and type(raw.get('observer_world_actor_id')) is int
                and raw['observer_world_actor_id'] in ids
                and isinstance(raw.get('reason'), str) and raw['reason'].strip()):
            bounded = L._safe_json_evidence(raw['reason'].strip())
            rejection = {'observer_world_actor_id': raw['observer_world_actor_id'],
                         'reason': bounded if isinstance(bounded, str) else 'learning_review_deferred'}
            if not isinstance(bounded, str):
                rejection['reason_evidence'] = bounded
            rejected.append(rejection)
        else:
            rejected.append({'reason': 'learning_review_deferred_invalid', 'raw_record': L._safe_json_evidence(raw)})
    return accepted, rejected


def build_design_view(learning_input, reviewed):
    problems = []
    known = {(t['world_actor_id'], r['knowledge_id']): r
             for t in learning_input['existing_knowledge'] for r in t['countermeasure_knowledge']}
    for row in reviewed:
        prior = known.get((row['observer_world_actor_id'], row['matched_knowledge_id']))
        problems.append({
            'review_index': row['review_index'], 'observer_world_actor_id': row['observer_world_actor_id'],
            'recognized_method': row['learned_boundary'], 'learning_feedback': row['learning_feedback'],
            'unresolved_reason': row['unresolved_reason'],
            'existing_knowledge': {k: deepcopy(prior[k]) for k in (
                'knowledge_level', 'audience_type', 'learned_boundary', 'countermeasure_direction',
            ) if k in prior} if prior else None,
            'direction_max_chars': max(0, learning_input['memory_max_chars'] - len('\n'.join(
                row[k] for k in ('encountered_method', 'observed_effect', 'unresolved_reason', 'learned_boundary')
                if row[k])) - 1),
        })
    actors = []
    wanted = {r['observer_world_actor_id'] for r in reviewed}
    for actor in learning_input['actors']:
        if actor['id'] not in wanted:
            continue
        # Frozen text may carry unique principles and goals absent from the
        # structured snapshot. Keep it until those fields have a lossless source.
        actors.append(deepcopy(actor))
    view = {'actors': actors, 'problems': problems}
    if 'actor_identity_scope' in learning_input:
        view['actor_identity_scope'] = learning_input['actor_identity_scope']
    return view


def normalize_design(value, reviewed, learning_input, *, sparse_updates=False):
    from . import s1_countermeasure_learning_ai_service as L
    if (not isinstance(value, dict) or not isinstance(value.get('adaptations'), list)
            or (not sparse_updates and not isinstance(value.get('unchanged'), list))):
        raise ValueError('learning_design_arrays_required')
    by_index = {r['review_index']: r for r in reviewed}
    known = {(target['world_actor_id'], target['capability_revision'], row['knowledge_id']): row
             for target in learning_input['existing_knowledge']
             for row in target['countermeasure_knowledge']} if sparse_updates else {}
    occurrences, rejected = {}, []
    for field in (('adaptations',) if sparse_updates else ('adaptations', 'unchanged')):
        for raw in value[field]:
            index = raw.get('review_index') if isinstance(raw, dict) else None
            if type(index) is not int or index not in by_index:
                rejected.append({'reason': 'learning_design_unknown_review', 'raw_record': L._safe_json_evidence(raw)})
            else:
                occurrences.setdefault(index, []).append((field, raw))
    proposed, indexes = [], []
    for index, source in by_index.items():
        entries = occurrences.get(index, [])
        if not entries and sparse_updates:
            prior = known.get((source['observer_world_actor_id'], source['observer_capability_revision'],
                               source['matched_knowledge_id']))
            if source['matched_knowledge_id'] is not None and prior is None:
                rejected.append({'review_index': index, 'reason': 'learning_design_baseline_missing'})
                continue
            personal = bool(prior and prior.get('audience_type') == 'actor'
                            and prior.get('audience_key') == f"actor:{source['observer_world_actor_id']}"
                            and prior.get('observer_world_actor_id') == source['observer_world_actor_id'])
            if prior and not personal and prior.get('audience_type') != 'faction':
                rejected.append({'review_index': index, 'reason': 'learning_design_baseline_owner_invalid'})
                continue
            entries = [('adaptations', {
                'knowledge_level': prior.get('knowledge_level') if personal else 'observed',
                'countermeasure_direction': prior.get('countermeasure_direction') if personal else '',
            })]
        if len(entries) != 1:
            rejected.append({'review_index': index, 'reason': 'learning_design_missing_or_duplicate'})
            continue
        field, raw = entries[0]
        if field == 'unchanged':
            if source['matched_knowledge_id'] is None or not isinstance(raw.get('reason'), str) or not raw['reason'].strip():
                rejected.append({'review_index': index, 'reason': 'learning_design_unchanged_invalid'})
            continue
        row = {k: deepcopy(source[k]) for k in (
            'observer_world_actor_id', 'matched_knowledge_id', *L._EXPERIENCE_FACT_FIELDS, 'learned_boundary',
        )}
        row.update(knowledge_level=raw.get('knowledge_level'), countermeasure_direction=raw.get('countermeasure_direction'))
        # The reviewed observation already has authority. An explicitly disabled
        # direction must not erase it or silently promote its knowledge level.
        if (row['knowledge_level'] == 'observed'
                and isinstance(row['countermeasure_direction'], str)
                and row['countermeasure_direction'].strip()):
            rejected.append({
                'review_index': index,
                'reason': 'observed_direction_quarantined',
                'observation_retained': True,
                'raw_record': L._safe_json_evidence(raw),
            })
            row['countermeasure_direction'] = ''
        proposed.append(row); indexes.append(index)
    accepted, errors = L.normalize_experiences({'experiences': proposed}, learning_input)
    for row in accepted:
        row['review_index'] = indexes[row['experience_index']]
    rejected.extend(errors)
    from .s1_countermeasure_growth_service import attach_proposals
    rejected.extend(attach_proposals(value, accepted, reviewed, learning_input))
    return accepted, rejected


def seal_review_sources(reviewed, experiences, learning_input):
    from . import s1_countermeasure_learning_ai_service as L
    by_index = {r['review_index']: r for r in reviewed}
    records = []
    for row in experiences:
        source = by_index.get(row.get('review_index'))
        if source is None or any(row.get(k) != source.get(k) for k in (
            'observer_world_actor_id', 'observer_capability_revision', 'matched_knowledge_id',
            *L._EXPERIENCE_FACT_FIELDS, 'learned_boundary',
        )):
            raise ValueError('learning_design_sealed_fact_mismatch')
        records.append({'experience_index': row['experience_index'], 'source_refs': source['source_refs'], 'reason': source['reason']})
    return L.seal_learning_archive({'records': records, 'deferred': []}, experiences, learning_input)
