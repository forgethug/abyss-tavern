"""Evidence-bound changes to a campaign's unentered future, never to its history."""
from copy import deepcopy
from hashlib import sha256
import json

from flask import current_app
from plugins.adventurer_s1.services.s1_campaign_stage_contract import normalize_stage_plan


def limit(name):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_CAMPAIGN_CONTINUATION_' + name
    return int(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()


def input_messages(snapshot, proposal=None):
    """Complete wire projection; the shared request gateway owns the input budget."""
    from plugins.adventurer_s1.services.s1_campaign_continuation_input import compact_input
    payload = compact_input(snapshot, proposal)
    system = REVIEW_RULE if proposal is not None else GENERATION_RULE
    content = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': content}]


def text(value, field, *, empty=False):
    if not isinstance(value, str) or len(value.strip()) > limit('TEXT_LIMIT') or (not empty and not value.strip()):
        raise ValueError(field + ' 文本无效或超长')
    return value.strip()


def evidence_ids(value, snapshot):
    allowed = {r['log_id'] for r in snapshot['evidence']}
    if (not isinstance(value, list) or not value or len(value) > len(allowed)
            or any(type(v) is not int or v not in allowed for v in value) or len(set(value)) != len(value)):
        raise ValueError('衔接必须引用本战役已冻结的准确战报')
    return sorted(value)


def normalize_proposal(raw, snapshot):
    if not isinstance(raw, dict) or raw.get('input_hash') != snapshot['input_hash']:
        raise ValueError('衔接提案的冻结输入身份不匹配')
    branch = raw.get('branch')
    if branch not in snapshot['eligible_branches']:
        raise ValueError('衔接分支尚未满足当前阶段条件')
    old = deepcopy(snapshot['plan'])
    entered = set(snapshot['entered_stage_keys'])
    raw_stages = raw.get('stages')
    if not isinstance(raw_stages, list) or len(raw_stages) > limit('MAX_STAGES'):
        raise ValueError('未来阶段调整数组无效')
    keys = [s.get('key') if isinstance(s, dict) else None for s in raw_stages]
    if any(not isinstance(k, str) or k in entered for k in keys) or len(set(keys)) != len(keys):
        raise ValueError('不能修改已进入阶段或重复调整同一阶段')
    by_key = {s['key']: s for s in old['stages']}
    for stage in raw_stages:
        by_key[stage['key']] = stage
    target = raw.get('next_stage_key')
    if not isinstance(target, str) or (target and (target not in by_key or target in entered)):
        raise ValueError('衔接出口必须为尚未进入的阶段，空字符串表示结束')
    current_key = snapshot['before']['stage_key']
    by_key[current_key][branch + '_next'] = target
    plan = {**old, 'stages': list(by_key.values())}
    maps = {f['base_dungeon'] for f in snapshot['fronts'] if f['campaign_role'] == 'dungeon_front'}
    # Older plans deliberately omit revelation content. Keep that established mode.
    plan['revelation_enabled'] = old.get('revelation_enabled', False)
    plan = normalize_stage_plan(plan, maps)
    plan['continuity_enabled'] = True
    # Normalization must not rewrite historic shapes, missing defaults, or challenges.
    for index, stage in enumerate(plan['stages']):
        if stage['key'] in entered:
            original = deepcopy(next(s for s in old['stages'] if s['key'] == stage['key']))
            if stage['key'] == current_key:
                original[branch + '_next'] = target
            plan['stages'][index] = original
    # A reachable cycle could replay an already settled phase indefinitely.
    visiting, visited = set(), set()
    normalized = {s['key']: s for s in plan['stages']}
    def visit(key):
        if not key:
            return
        if key in entered or key in visiting:
            raise ValueError('后续路线不能回到已进入阶段或形成循环')
        if key in visited:
            return
        visiting.add(key)
        for edge in ('success_next', 'failure_next'):
            visit(normalized[key][edge])
        visiting.remove(key)
        visited.add(key)
    visit(target)
    successors = raw.get('successors')
    if not isinstance(successors, list) or len(successors) > len(maps):
        raise ValueError('后续事件数组无效')
    fronts = {f['id']: f for f in snapshot['fronts'] if f['campaign_role'] == 'dungeon_front'}
    projected, seen, titles = [], set(), set()
    for row in successors:
        if not isinstance(row, dict):
            raise ValueError('后续事件必须为对象')
        source_id = row.get('predecessor_id')
        if type(source_id) is not int or source_id not in fronts or source_id in seen:
            raise ValueError('后续事件必须绑定唯一的当前地图前线')
        front = fronts[source_id]
        if type(row.get('source_revision')) is not int or row['source_revision'] != front['revision']:
            raise ValueError('后续事件前驱版本不匹配')
        if front['status'] not in ('resolved', 'dormant'):
            raise ValueError('可继续推进的前线沿用原线，不另建替代事件')
        title = text(row.get('title'), '后续事件标题')
        if len(title) > 120 or title in titles or title in {f['title'] for f in snapshot['fronts']}:
            raise ValueError('后续事件标题无效或与旧事件重复')
        ids = evidence_ids(row.get('evidence_log_ids'), snapshot)
        if not any(e['log_id'] in ids and e['dungeon'] == front['base_dungeon'] for e in snapshot['evidence']):
            raise ValueError('后续事件必须包含准确前驱地图的本地战报依据')
        projected.append({
            'predecessor_id': source_id, 'source_revision': row['source_revision'],
            'title': title, 'summary': text(row.get('summary'), '后续事件事实'),
            'campaign_agenda': text(row.get('campaign_agenda'), '后续事件议程'),
            'evidence_log_ids': ids,
        })
        seen.add(source_id)
        titles.add(title)
    # Every transition re-enters continuity with fresh evidence. Require executable
    # fronts for the stage being opened, not every possible later branch in advance.
    reachable_maps = {n['dungeon'] for key in visited for n in normalized[key]['nodes']}
    next_maps = {n['dungeon'] for n in normalized[target]['nodes']} if target else set()
    active = {f['base_dungeon'] for f in fronts.values()
              if f['status'] in ('seeded', 'active', 'escalated', 'closing')}
    active.update(fronts[r['predecessor_id']]['base_dungeon'] for r in projected)
    missing_maps = next_maps - active
    if missing_maps:
        raise ValueError(f'下一阶段 {target} 缺少可用前线或真实后继：' + '、'.join(sorted(missing_maps)))
    if any(fronts[r['predecessor_id']]['base_dungeon'] not in reachable_maps for r in projected):
        raise ValueError('后续事件必须服务于本次后续路线')
    return {
        'input_hash': snapshot['input_hash'], 'branch': branch, 'next_stage_key': target,
        'stages': [deepcopy(normalized[k]) for k in keys],
        'successors': projected, 'evidence_log_ids': evidence_ids(raw.get('evidence_log_ids'), snapshot),
        'reason': text(raw.get('reason'), '衔接依据'),
        'public_summary': text(raw.get('public_summary'), '公开衔接说明'),
    }


def updated_plan(proposal, snapshot):
    plan = deepcopy(snapshot['plan'])
    by_key = {s['key']: s for s in plan['stages']}
    for stage in proposal['stages']:
        by_key[stage['key']] = deepcopy(stage)
    by_key[snapshot['before']['stage_key']][proposal['branch'] + '_next'] = proposal['next_stage_key']
    plan['stages'] = list(by_key.values())
    plan['continuity_enabled'] = True
    return plan


def _action_window_bindings(plan):
    return {(stage['key'], node['key'], node['dungeon']): node['action_window']
            for stage in plan['stages'] for node in stage['nodes']
            if node.get('action_window') is not None}


def _challenge_bindings(plan):
    return {(stage['key'], node['key'], node['dungeon']): node['challenge']
            for stage in plan['stages'] for node in stage['nodes']
            if node.get('challenge') is not None}


def normalize_generated_proposal(raw, snapshot):
    from plugins.adventurer_s1.services.s1_campaign_continuation_input import inherit_generated_fields
    raw = inherit_generated_fields(raw, snapshot)
    proposal = normalize_proposal(raw, snapshot)
    configured = {s['key']: s['completion_nodes'] for s in snapshot['plan']['stages']
                  if 'completion_nodes' in s}
    resulting = {s['key']: s.get('completion_nodes') for s in updated_plan(proposal, snapshot)['stages']}
    if any(resulting.get(key) != nodes for key, nodes in configured.items()):
        raise ValueError('自动衔接必须保留运营配置的关键目标绑定')
    if (_action_window_bindings(updated_plan(proposal, snapshot))
            != _action_window_bindings(snapshot['plan'])):
        raise ValueError('自动衔接不能新增、删除、改写或搬移运营行动窗口及其阶段、节点、地图绑定')
    if _challenge_bindings(updated_plan(proposal, snapshot)) != _challenge_bindings(snapshot['plan']):
        raise ValueError('自动衔接不能新增、删除、改写或搬移运营挑战及其阶段、节点、地图绑定')
    return proposal


def normalize_review(raw, snapshot, proposal):
    if (not isinstance(raw, dict) or raw.get('input_hash') != snapshot['input_hash']
            or raw.get('proposal_hash') != digest(proposal)
            or raw.get('decision') not in ('allow', 'reject')):
        raise ValueError('阶段衔接审核身份或结论无效')
    return {'input_hash': snapshot['input_hash'], 'proposal_hash': digest(proposal),
            'decision': raw['decision'], 'reason': text(raw.get('reason'), '审核依据')}


CONTINUITY_RULE = '''
节点 challenge_context 为运营挑战的规划视图，不是可写配置；已继承挑战展示等级、公开概述、主体状态、战场条件与准备行动的因果关系，未列出的详细能力不代表不存在，不能据此改写战术效果或重设能力。当前阶段已结算节点仅展示身份和目标，结论以 before.progress 与战报为准；未完成节点仍提供规划条件。完整挑战和行动窗口由后端保持，正式出发再按原链路冻结；管理员确实改写的挑战在复核提案中完整提供。人物 current_state、power_boundary 与战报结果约束目标可执行性，本职责不重新设计战斗技能。
plan.continuation_directive 是运营授权的后续编排要求，只约束未来任务安排，不建立世界事实或放宽证据与身份权限。阶段可配置 completion_nodes：这是决定成功与失败分支的关键任务 key 非空去重数组，其余任务为可选行动；省略时所有节点均为关键任务。可选行动的真实战果、损失和未尽后果仍承接，但不要求将它们全部完成才能转段或结束。当前阶段未完成的可选事项不能写成已完成；有后续阶段且事项仍可执行时作为可选行动承接，已自然失去行动条件时记录原因。成功与失败终局各按下述证据条件收束，不以已执行次数或地图数量推断结束。已有运营 completion_nodes 绑定保持不变；未来阶段未配置该字段时可依据真实目标新增配置。终局条件已经成立时可直接整理公开结局，不为填写收尾清单再安排强制出征。
共同战役阶段衔接消费已经通过发布链路的战报结论（evidence.report）、精确前线当前态、人物与势力现状及当前和未来运营计划；完整旧正文留在后台校验，不在本职责重新审核探险。report.node_evidence 与公开战报只证明其明确记载的结果，省略的细节未知；不得据缺失细节复活主体、补造去向、提前结案，必要核验仍作为任务。只调整未进入阶段以及当前已满足分支的后续出口，不重写历史。未来编排优先遵循 continuation_directive，未涉及之处沿用仍可执行的原计划；事实、公开知识、精确身份及运营配置保护优先。提前完成计入真实成果，后续只保留尚未完成且可验证的目标。任务安排和待查可能性不成为已发生事实。
local_world.locations 保存本区战役历次前线或已发布任务通过持久地点 ID 准确关联的非空地点现状，以及关联地图本身已有的汇总态；它不是整张地图的地点目录，未列出的区域仍属未核实，不能据此推断完好、空置或不存在。local_world.actors 保留与战役地图存在准确历史关联的人物档案，包括尚未加入本战役的人物。它们与本战役战报共同约束未来任务的可执行性：已毁地点、失效通路及人物当前生死、去向与能力须承接；历史地图关联和 scope_keys 不证明人物此刻在场、知情或参与战役。使用精确身份，不按名称相似度合并实体，不为利用档案强制安排出场。本区事实独立决定后续，不要求与另一分区产生差异。
timeline、战报 created_at/published_at 和 action_window_context 只提供本区真实时序。时间阈值不证明前提一直有效或预定过程已经发生；只承接战报结论和当前事实建立的过程与阻断，未核实的时间后果保留待查。阶段衔接不执行时间事件，不修改地点、人物或资产。local_world 和窗口条件是后台证据，不自动进入公开说明。
每项替换、跳过、结束和后继须引用战报锚定战役历史、阶段及已取得成果；冻结的 local_world 可补充证明未来目标是否仍可执行。当地独立事件已改变目标时据此调整后续，不要求旧战报已经记载该变化，不改写旧战报，也不把其他行动的成果计作本队完成。仍未履行的目标继续承接。已死亡、被俘、撤离的人物及其伤势、装备与护卫损耗、已毁据点、货物数量和实际去向均保持有效。后续难度配置只约束尚未确立的能力，不复原损失，不生成第二份已追回或毁失的货物，不为填充阶段新增威胁。
后继只从冻结的 resolved/dormant 地图前驱建立，summary 记录既成结果留下的真实未完成事项；campaign_agenda 描述接下来的查证或处置。前驱的终态保持有效，同一战役与地图归属由后端继承。可推进前线沿用原线。成功终局依据关键目标与整体威胁实际收束的证据，不要求可选目标全部完成；失败终局依据已成立的不可挽回失败及其真实后果，允许威胁仍存在，不能宣称胜利。证据不足时保留必要核验，不宣布完成。
next_stage_key 指向的下一阶段各地图必须有可用前线或本次有据后继；更后阶段在实际进入前按届时事实重新衔接，不要求本次提前补齐其后继。已提供的后继仍须服务于可达后续路线并逐项核验证据。
阶段 failure_next 为阶段 key 时表示失败后转入该阶段，为空字符串时表示失败后结束战役，为 null 时表示没有失败出口。任一关键任务的 failure_condition 非空时，该阶段必须配置失败出口；关键任务按 completion_nodes 确定，省略时为全部节点。没有失败出口时，关键任务的 failure_condition 应为空字符串；仅可选任务配置失败条件不要求阶段失败出口。出口和失败条件须共同承接真实目标与证据，失败后结束仍须满足上述失败终局依据。
public_summary 只整理酒馆已收到的情报与当前行动理由，不泄露未来阶段、未证实人物位置或私有挑战。本区后台事实尚未传播时可约束规划，但不直接进入公告；公开结论仍需确认时安排必要核验，不把私有现状直接写成酒馆已知。若启用 revelation，阶段 public_title 与节点 investigation 同样遵循已公开知识，clue_dungeons 只能列当前阶段配置的地图。
'''.strip()

GENERATION_RULE = CONTINUITY_RULE + '''
输出单个 JSON 对象，字段全部显式给出：
input_hash 原样复制输入；branch 从 eligible_branches 选择；next_stage_key 为尚未进入阶段的准确 key，空字符串表示战役结束；
stages 只列需要替换或新增的未进入阶段，无变化为 []。每项字段 key/title/nodes/success_next/failure_next，nodes 每项 key/dungeon/title/objective/success_condition/failure_condition；启用 revelation 时还须阶段 public_title 和节点 revelation={discovery,investigation,clue_dungeons}。success_next 空字符串表示结束；failure_next 按上述失败出口合同填写。地图来自冻结前线，任务与阶段 key 唯一且符合小写字母数字及 ._-。已有节点的 challenge、action_window 和已有 completion_nodes 由后端按准确 stage.key、node.key、dungeon 继承，不输出这些受保护字段，不删除或搬移其节点；未配置 completion_nodes 的未进入阶段（含既有或新增阶段）可输出该字段。有据跳过带配置阶段只调整路线出口，不回写整份计划。
successors 为后继数组，无需新后继为 []；每项 predecessor_id/source_revision 精确复制前驱，title 为独立标题，summary/campaign_agenda 为非空文本，evidence_log_ids 为支持该后继的准确战报 ID 数组。
evidence_log_ids 为整体衔接依据的战报 ID 数组；reason 为后台依据；public_summary 为公开衔接说明。
仅输出被消费的变化，保留未调整阶段及所有已进入阶段；不要输出当前进度或人物、资产、地点的修改字段。
数组数量与文本长度服从 contract_limits：stages 可空且 key 不重复，合并后的阶段总数和每阶段 nodes 数量各有上限；successors 可空且 predecessor_id、title 各自唯一。evidence_log_ids 非空、去重，只引用冻结 evidence，并受其总量上限约束；每项后继至少引用一份其前驱地图的本地战报。每阶段 dungeon 与 node.key 分别唯一。超过上限时不输出截断或部分提案。
'''.strip()

REVIEW_RULE = CONTINUITY_RULE + '''
你复核冻结阶段衔接提案，不改写提案。逐项核对其证据引用、未来目标覆盖、已完成成果保留、后继因果、目标与路线和已提供的力量边界/损耗/战场准备条件的一致性及公开知识边界；不重审省略的战斗技能细节。放行只代表整份提案均可依据冻结输入执行；存在任一无据跳过、提前结束、虚构后继或世界终态冲突即 reject，并说明具体缺口。不因文风或偏好拒绝。
输出单个 JSON 对象：input_hash、proposal_hash 原样复制本次准确值；decision 为 allow 或 reject；reason 为非空依据。该出口只允许或拒绝完整阶段衔接，不授权修改旧战报、结算或世界终态。
'''.strip()
