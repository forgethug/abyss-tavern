"""Stage definitions and deterministic invitation gates; world facts stay with their owner."""
from __future__ import annotations

from copy import deepcopy
import re

from flask import current_app
from plugins.adventurer_s1.services.s1_campaign_challenge_contract import normalize_challenge
from plugins.adventurer_s1.services.s1_campaign_time_contract import normalize_action_window

RESULTS = frozenset({'pending', 'confirmed', 'failed'})
ADVANCE_MODES = frozenset({'auto', 'manual'})


def advance_mode(plan):
    # Plans saved before the switch was introduced were explicitly manual.
    return 'auto' if plan.get('advance_mode') == 'auto' else 'manual'


def _limit(name):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_CAMPAIGN_STAGE_' + name
    return int(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))


def _text(value, field, limit, *, empty=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (not empty and not value.strip()):
        raise ValueError(f'{field} 必须为{"可空" if empty else "非空"}文本，最多 {limit} 字')
    return value.strip()


def _key(value, field):
    value = _text(value, field, 64)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', value):
        raise ValueError(f'{field} 必须为稳定的小写字母、数字或 ._- 标识')
    return value


def normalize_stage_plan(value, allowed_dungeons):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('stage_plan 必须为对象')
    continuity = value.get('continuity_enabled', False)
    if type(continuity) is not bool:
        raise ValueError('continuity_enabled 必须为布尔值')
    reveal = value.get('revelation_enabled', True)
    if type(reveal) is not bool:
        raise ValueError('revelation_enabled 必须为布尔值')
    mode = value.get('advance_mode', 'auto')
    if not isinstance(mode, str) or mode not in ADVANCE_MODES:
        raise ValueError('advance_mode 必须为 auto 或 manual')
    raw_stages = value.get('stages')
    if not isinstance(raw_stages, list) or not 1 <= len(raw_stages) <= _limit('MAX_STAGES'):
        raise ValueError('阶段数量超出配置范围')
    stages = []
    for stage_index, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            raise ValueError('阶段必须为对象')
        nodes = raw.get('nodes')
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= _limit('MAX_NODES'):
            raise ValueError('阶段地图任务数量超出配置范围')
        projected = []
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError('地图任务必须为对象')
            dungeon = _text(node.get('dungeon'), 'dungeon', 100)
            if dungeon not in allowed_dungeons:
                raise ValueError('阶段地图必须已有对应的战役副本前线')
            projected.append({
                'key': _key(node.get('key'), 'node.key'), 'dungeon': dungeon,
                'title': _text(node.get('title'), 'node.title', 120),
                'objective': _text(node.get('objective'), 'node.objective', 1200),
                'success_condition': _text(node.get('success_condition'), 'success_condition', 1200),
                'failure_condition': _text(node.get('failure_condition', ''), 'failure_condition', 1200, empty=True),
            })
            if node.get('challenge') is not None:
                projected[-1]['challenge'] = normalize_challenge(node['challenge'])
            if node.get('action_window') is not None:
                projected[-1]['action_window'] = normalize_action_window(node['action_window'])
            if reveal:
                content = node.get('revelation')
                if not isinstance(content, dict):
                    raise ValueError('地图任务必须配置 revelation 本地揭示内容')
                clues = content.get('clue_dungeons', [])
                if (not isinstance(clues, list) or any(not isinstance(name, str) for name in clues)
                        or len(set(clues)) != len(clues)):
                    raise ValueError('clue_dungeons 必须为不重复的准确地图名称数组')
                projected[-1]['revelation'] = {
                    'discovery': _text(content.get('discovery', ''), 'revelation.discovery', 1200,
                                       empty=stage_index > 0),
                    'investigation': _text(content.get('investigation'), 'revelation.investigation', 1200),
                    'clue_dungeons': clues,
                }
        if len({n['key'] for n in projected}) != len(projected) or len({n['dungeon'] for n in projected}) != len(projected):
            raise ValueError('同一阶段的任务标识与地图必须分别唯一')
        success_next = raw.get('success_next', '')
        failure_next = raw.get('failure_next')
        stages.append({
            'key': _key(raw.get('key'), 'stage.key'),
            'title': _text(raw.get('title'), 'stage.title', 120),
            'nodes': projected,
            'success_next': _key(success_next, 'success_next') if success_next else '',
            'failure_next': _key(failure_next, 'failure_next') if failure_next else ('' if failure_next == '' else None),
        })
        if 'completion_nodes' in raw:
            stages[-1]['completion_nodes'] = normalize_completion_nodes(raw['completion_nodes'], projected)
        if reveal:
            stages[-1]['public_title'] = _text(raw.get('public_title'), 'stage.public_title', 120)
            stage_maps = {n['dungeon'] for n in projected}
            if any(not set(n['revelation']['clue_dungeons']).issubset(stage_maps) for n in projected):
                raise ValueError('线索只能指向当前阶段已经配置任务的准确地图')
        if not isinstance(success_next, str) or (failure_next is not None and not isinstance(failure_next, str)):
            raise ValueError('阶段出口必须为阶段标识，空字符串表示结束；failure_next=null 表示没有失败出口')
        if failure_next is None:
            required_keys = completion_node_keys(stages[-1])
            for node in projected:
                if node['key'] in required_keys and node['failure_condition']:
                    raise ValueError(
                        '配置不可挽回的失败条件时必须同时配置失败出口：'
                        f"stage.key={stages[-1]['key']}，node.key={node['key']}，"
                        'failure_condition 非空但 failure_next=null（或缺失）')
    keys = {s['key'] for s in stages}
    if len(keys) != len(stages):
        raise ValueError('阶段标识必须唯一')
    for stage in stages:
        if any(target and target not in keys for target in (stage['success_next'], stage['failure_next'])):
            raise ValueError('阶段出口指向不存在的阶段')
    result = {'advance_mode': mode, 'revelation_enabled': reveal, 'stages': stages}
    if 'continuity_enabled' in value:
        result['continuity_enabled'] = continuity
    if 'continuation_directive' in value:
        result['continuation_directive'] = _text(
            value['continuation_directive'], 'continuation_directive', 1200, empty=True)
    return result


def stage_definition(plan, key):
    return next(stage for stage in plan['stages'] if stage['key'] == key)


def initial_progress(stage, *, revealed=False):
    return {node['key']: {'knowledge': 'hinted' if revealed else 'hidden',
                         'result': 'pending', 'evidence_log_id': None,
                         'evidence_revision': None, 'evidence_text': ''}
            for node in stage['nodes']}


def normalize_completion_nodes(value, nodes):
    keys = {node['key'] for node in nodes}
    if (not isinstance(value, list) or not value or len(value) > len(keys)
            or any(not isinstance(key, str) or key not in keys for key in value)
            or len(set(value)) != len(value)):
        raise ValueError('completion_nodes 必须为本阶段不重复、非空的准确任务标识数组')
    return [node['key'] for node in nodes if node['key'] in value]


def completion_node_keys(stage):
    # Saved plans without this field retain the original all-node gate.
    return stage.get('completion_nodes', [node['key'] for node in stage['nodes']])


def eligible_branches(stage, progress):
    values = [progress[key]['result'] for key in completion_node_keys(stage)]
    branches = []
    if values and all(value == 'confirmed' for value in values):
        branches.append('success')
    if stage['failure_next'] is not None and any(value == 'failed' for value in values):
        branches.append('failure')
    return branches


def available_stage_maps(stage, progress):
    return [node['dungeon'] for node in stage['nodes']
            if progress[node['key']]['knowledge'] != 'hidden'
            and progress[node['key']]['result'] == 'pending']


def project_node_result(report, *, discovery=False):
    result = report.get('node_result')
    if not isinstance(result, str) or result not in RESULTS:
        raise ValueError('阶段战报 node_result 必须为 pending/confirmed/failed')
    # Before the campaign's first discovery is public, an encounter can establish
    # facts and reveal tasks, but has no authority to settle a node. This also
    # projects pre-upgrade pending candidates without discarding their evidence.
    return {'node_result': 'pending' if discovery else result,
            'node_evidence': _text(report.get('node_evidence'), 'node_evidence', 1200)}


def validate_node_result(report, task, *, discovery=False):
    value = project_node_result(report, discovery=discovery)
    if value['node_result'] == 'failed' and not task['failure_condition']:
        raise ValueError('本任务没有不可挽回的失败条件；受阻时应保留 pending')
    return value


def apply_node_report(stage, progress, task, report, log_id, revision, *, discovery=False):
    result = deepcopy(progress)
    key = task['node_key']
    value = validate_node_result(report, task, discovery=discovery)
    result[key] = {'knowledge': 'discovered', 'result': value['node_result'],
                   'evidence_log_id': log_id, 'evidence_revision': revision,
                   'evidence_text': value['node_evidence']}
    # Public destination fields already require reportable evidence. They reveal
    # an investigation direction, never remotely confirm a map's local objective.
    for node in stage['nodes']:
        entry = result[node['key']]
        if node['dungeon'] in report['target_dungeons'] and entry['knowledge'] == 'hidden':
            entry.update(knowledge='hinted', evidence_log_id=log_id,
                         evidence_revision=revision, evidence_text=report['public_summary'])
    return result


STAGE_GENERATION_RULE = '''
冻结 stage_task 绑定本场唯一的阶段实例与地图节点。同源 campaign_dispatch 必须包含 node_result 与 node_evidence，后者为非空的本场事实依据，最多1200字。discovery=true 时，本场职责是建立可传播的发现，stage_task 仅提供阶段实例与节点身份；outcome=discovered，node_result=pending，node_evidence 记录实际发现。发现发布后才开放正式调查任务，本场已经取得的证据与实际变化完整承接到后续行动。
discovery=false 时，stage_task 还提供正式任务的 title、objective、success_condition 和可空的 failure_condition。按正文已成立的结果判断：node_result=confirmed 表示 success_condition 已被本场证据满足，failed 表示非空 failure_condition 已实际成立，pending 表示尚需继续；本次行动成败不等同于地图确认或整个阶段结束。没有冻结 stage_task 时省略节点字段。
阶段任务的 next_objective 与 target_dungeons 只提供当前阶段内有可传播依据的后续方向；有提案时二者同时非空，没有新提案时同时为空。它们不决定阶段开放或整个事件结束。地图外的线索只建立追查方向，不能替代该地图尚未发生的本地调查。阶段开放由后端已确认进度负责，本场不预写下一阶段或未来结局；既已发生的实际变化仍完整保存。地图任务完成不等于整条战役前线结束；同源前线仍有实际因果可继续时使用 advance/successor，已经自然结束时沿世界事实合同 close/resolve。阶段计划只表示运营安排，不授权延长、恢复或改变已成立的世界终态。仅冻结来源的一条 world_fact_updates 携带这份报告，其他合法事实仍完整保留。
报告结构占位（本例无后续提案）：{"public_title":"<非空公开标题>","public_summary":"<本场可传播事实>","outcome":"ongoing","next_objective":"","target_dungeons":[],"node_result":"pending","node_evidence":"<本场事实依据>"}。首次发现使用 discovered；正式任务按前述冻结条件判断节点结果。node_evidence 为后台核验依据，公开字段另受既有知识边界约束。
'''.strip()

STAGE_OBSERVER_RULE = '''
同一冻结条目含 stage_task 时，先按 discovery 区分发现与正式任务职责，再用最终正文和对应世界事实核对节点依据。首次发现的公开战报可完整记录实际线索与世界变化，节点结果保持 pending；正式任务才核对冻结确认条件。公告遗漏、把首次发现宣告为正式调查任务已结算、节点结论不受证据支持或把跨地图线索当作当地任务完成时，通过既有完整 override_fields.world_fact_updates 出口修正同源公告并保留其它合法事实，不为满足阶段条件改写正文。按阶段任务合同核对当前阶段提案和同源前线继续/收束规则。
'''.strip()
