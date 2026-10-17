"""Frozen, phase-scoped campaign knowledge; the full operator plan stays private."""
from __future__ import annotations
from copy import deepcopy


def revelation_enabled(plan):
    # Persisted plans predating this contract retain their original behavior.
    return plan.get('revelation_enabled') is True


def public_stage_title(plan, stage):
    return stage['public_title'] if revelation_enabled(plan) else stage['title']


def update_unrevealed_revelations(plan, patches, current_stage_key, progress, entered_stage_keys):
    """Edit private clues without rewriting public knowledge or the original plan."""
    if not revelation_enabled(plan):
        raise ValueError('该战役未启用逐步揭示，不能调整本地线索')
    if not isinstance(patches, list) or not patches:
        raise ValueError('请提供待调整的未揭示线索')
    nodes = {(s['key'], n['key']): n for s in plan['stages'] for n in s['nodes']}
    if len(patches) > len(nodes):
        raise ValueError('线索调整数量超过阶段任务数')
    candidate = deepcopy(plan)
    targets = {(s['key'], n['key']): n for s in candidate['stages'] for n in s['nodes']}
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict) or any(not isinstance(patch.get(k), str) for k in ('stage_key', 'node_key')):
            raise ValueError('线索调整必须引用准确阶段与节点标识')
        identity = (patch['stage_key'], patch['node_key'])
        if identity not in nodes or identity in seen or not isinstance(patch.get('revelation'), dict):
            raise ValueError('线索调整的节点不存在、重复或缺少完整配置')
        if identity[0] == current_stage_key:
            entry = progress.get(identity[1]) or {}
            if (entry.get('knowledge') != 'hidden' or entry.get('result') != 'pending'
                    or entry.get('evidence_log_id') is not None or entry.get('evidence_revision') is not None
                    or entry.get('evidence_text')):
                raise ValueError('已经揭示或已有经历证据的地图不能改写线索')
        elif identity[0] in entered_stage_keys:
            raise ValueError('历史阶段不能改写线索')
        seen.add(identity)
        targets[identity]['revelation'] = patch['revelation']
    # Reuse the creation contract once for the entire bounded batch. Only copy
    # its revelation projection back; unrelated legacy fields remain verbatim.
    from plugins.adventurer_s1.services.s1_campaign_stage_contract import normalize_stage_plan
    normalized = normalize_stage_plan(candidate, {n['dungeon'] for n in nodes.values()})
    projected = {(s['key'], n['key']): n for s in normalized['stages'] for n in s['nodes']}
    updated = deepcopy(plan)
    targets = {(s['key'], n['key']): n for s in updated['stages'] for n in s['nodes']}
    changes = []
    for identity in sorted(seen):
        old, value = nodes[identity]['revelation'], projected[identity]['revelation']
        if old != value:
            changes.append({'stage_key': identity[0], 'node_key': identity[1],
                            'dungeon': nodes[identity]['dungeon'], 'before': deepcopy(old), 'after': value})
            targets[identity]['revelation'] = value
    if not changes:
        raise ValueError('线索配置没有变化')
    return updated, changes


def project_revelation_context(value):
    """Fail closed on a damaged frozen boundary, while discarding unused extras."""
    if not isinstance(value, dict):
        raise ValueError('战役揭示范围必须为冻结对象')
    result = {}
    for key in ('local_facts', 'local_title'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'战役揭示范围缺少 {key}')
        result[key] = value[key]
    reports = value.get('known_reports')
    if not isinstance(reports, list):
        raise ValueError('战役已公开信息必须为数组')
    result['known_reports'] = []
    for report in reports:
        if not isinstance(report, dict) or any(
            not isinstance(report.get(key), str) or not report[key].strip()
            for key in ('dungeon', 'title', 'summary')
        ):
            raise ValueError('战役已公开信息不完整')
        result['known_reports'].append({key: report[key] for key in ('dungeon', 'title', 'summary')})
    return result


def freeze_revelation_context(context, stage, node, progress, published):
    """Use exact configured destinations, never infer knowledge from prose."""
    discovery = context['discovery'] is True
    reveal = node['revelation']
    known_maps = {n['dungeon'] for n in stage['nodes'] if progress[n['key']]['knowledge'] != 'hidden'}
    destinations = {node['dungeon']} if discovery else known_maps | set(reveal['clue_dungeons']) | {node['dungeon']}
    context['allowed_dungeons'] = [name for name in context['allowed_dungeons'] if name in destinations]
    context['revelation_context'] = project_revelation_context({
        'local_title': '当地事件的发现' if discovery else node['title'],
        'local_facts': reveal['discovery' if discovery else 'investigation'],
        'known_reports': [
            {'dungeon': name, 'title': report['public_title'], 'summary': report['public_summary']}
            for name, report in sorted(published.items())
        ],
    })
    # Keep the latest applied state of this map across stage changes and returns.
    # Other maps contribute their public reports, not their private current state.
    local_report = published.get(node['dungeon'])
    if local_report and (not isinstance(local_report.get('applied_world_state'), str)
                         or not local_report['applied_world_state'].strip()):
        raise ValueError('战役本地已发布当前态缺失，不能退回后台初始设定')
    context['current_world_state'] = local_report['applied_world_state'] if local_report else ''


def project_revealed_thread(thread, context):
    """One allowlist for narrative, outline selection and Observer snapshots."""
    reveal = project_revelation_context(context['revelation_context'])
    result = {key: thread[key] for key in (
        'id', 'revision', 'thread_type', 'status', 'base_dungeon',
        'campaign_root_id', 'campaign_role', 'is_chain_head',
    ) if key in thread}
    from plugins.adventurer_s1.services.s1_expedition_locus_service import extract_location_scope_keys
    result['scope_keys'] = [thread['base_dungeon'], *sorted(extract_location_scope_keys(thread.get('scope_keys')))]
    result.update(title=reveal['local_title'], summary=reveal['local_facts'])
    return result


REVELATION_GENERATION_RULE = '''
本场战役的可揭示事实由冻结 revelation_context.local_facts 提供，known_reports 是此前各地已经公开的最近战报；current_world_state 保留本地图最近已应用的真实变化，优先承接其中仍有效的后果，未公开部分不自行取得新增揭示权限。初始设定不能覆盖已成立的世界变化。人物仍须通过本场可成立的观察、交流或调查取得新知识；可揭示不等于自动得知，也不保证调查成功。
后台主轴、未来阶段、未开放节点和全路线不属于本场资料。仅按当前事实与已公开证据建立战役关联、主体身份和地点用途；地图名称、难度、角色能力档案及任务确认条件都不单独证明某地承担终局或某人是幕后首领。普通推测保留其不确定性，不为范围外的推测补造确证依据。首次发现只揭开本地事件；正式调查取得足以辨认具体去处、且可传播的证据时，才可指向 allowed_dungeons 中的新地图。allowed_dungeons 仅限定允许建立关联的范围，不是人物已知的路线。尚只能辨认环境、用途或相似痕迹时，保留为可继续追查的疑点，不据此开放具体地图；本场证据与 known_reports 的真实对应可以建立新关联，明确获知的去处也不必等待另一队到场才公开。远处地图的内幕与本地结果仍需在那里调查。公开阶段名、任务标题和战报沿用当前已知范围，不预告后台地图总数、后续阶段顺序或结局。
'''.strip()

REVELATION_OBSERVER_RULE = '''
冻结条目含 revelation_context 时，按同一可揭示范围核对正文和公开公告。仅公告超出正文可传播信息时，保持 needs_regeneration=false，以完整 override_fields.world_fact_updates 修正同源报告并保留其他合法事实；仅有模糊方向而公告开放具体地图时，按既有成对提案合同修正 next_objective / target_dungeons，疑点继续保留在公开摘要。若正文已无依据坐实战役关联、具体去处、幕后身份或终局地点，改变了调查所需的关键知识，且公告或结算补丁无法消除，则使用既有 needs_regeneration=true、regeneration_basis=other_unrepairable 和 regeneration_evidence.main_event_indexes 指向实际泄露事件。既有公开事实、合法本地发现和明确保留不确定性的推测正常承接；不因文风、线索显眼、一次取得明确去处或玩家可能猜到后续而重生成。
'''.strip()
