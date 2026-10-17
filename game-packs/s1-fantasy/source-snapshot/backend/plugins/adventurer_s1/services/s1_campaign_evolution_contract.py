"""Reviewed autonomous deltas; elapsed time never establishes a player result."""
from copy import deepcopy
import json
from plugins.adventurer_s1.services.s1_campaign_continuation_contract import digest
from plugins.adventurer_s1.services.s1_world_location_state_contract import (
    LOCATION_STATE_TEXT_MAX_LENGTH, LOCATION_CHANGE_SUMMARY_MAX_LENGTH,
)


def config(name):
    from flask import current_app
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_CAMPAIGN_EVOLUTION_' + name
    return current_app.config.get(key, AdventurerS1Plugin.default_config[key])


def _text(value, name, maximum=None):
    maximum = maximum or int(config('TEXT_LIMIT'))
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError('campaign_evolution_text_invalid:' + name)
    return value.strip()


def _array(raw, key, maximum):
    value = raw.get(key)
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(r, dict) for r in value):
        raise ValueError('campaign_evolution_array_invalid:' + key)
    return value


def _identity(value, allowed, seen):
    if type(value) is not int or value not in allowed or value in seen:
        raise ValueError('campaign_evolution_identity_invalid')
    seen.add(value)
    return allowed[value]


def normalize_proposal(raw, snapshot):
    if not isinstance(raw, dict) or raw.get('input_hash') != snapshot['input_hash']:
        raise ValueError('campaign_evolution_input_mismatch')
    disposition = raw.get('disposition')
    if not isinstance(disposition, str) or disposition not in ('changed', 'quiet'):
        raise ValueError('campaign_evolution_disposition_invalid')
    fronts = {r['id']: r for r in snapshot['fronts'] if r['campaign_role'] == 'dungeon_front'
              and r['status'] in ('seeded', 'active', 'escalated', 'closing')}
    updates, seen = [], set()
    for row in _array(raw, 'front_updates', len(fronts)):
        source = _identity(row.get('thread_id'), fronts, seen)
        summary = _text(row.get('summary'), 'summary')
        if summary == source['summary']:
            raise ValueError('campaign_evolution_front_unchanged')
        updates.append({'thread_id': source['id'], 'summary': summary})
    changed_maps = {fronts[row['thread_id']]['base_dungeon'] for row in updates}
    locations = {r['id']: r for r in snapshot['world']['locations']}
    location_updates, seen = [], set()
    for row in _array(raw, 'location_updates', len(locations)):
        source = _identity(row.get('location_id'), locations, seen)
        if (source['target_dungeon'] not in changed_maps or not source['write_available']
                or row.get('location_name') != source['canonical_name']
                or type(row.get('expected_revision')) is not int
                or row['expected_revision'] != source['revision']):
            raise ValueError('campaign_evolution_location_authority_invalid')
        new_state = _text(row.get('new_state'), 'new_state', LOCATION_STATE_TEXT_MAX_LENGTH)
        if new_state == source['current_state']:
            raise ValueError('campaign_evolution_location_unchanged')
        location_updates.append({'location_id': source['id'], 'location_name': source['canonical_name'],
            'expected_revision': source['revision'], 'new_state': new_state,
            'change_summary': _text(row.get('change_summary'), 'change_summary', LOCATION_CHANGE_SUMMARY_MAX_LENGTH)})
    actors = {r['id']: r for r in snapshot['world']['actors'] if r['status'] == 'active' and r['write_available']}
    actor_updates, seen = [], set()
    for row in _array(raw, 'actor_updates', len(actors)):
        source = _identity(row.get('actor_id'), actors, seen)
        state = _text(row.get('current_state'), 'current_state')
        agenda = _text(row.get('current_agenda'), 'current_agenda')
        if state == source['current_state'] and agenda == source['current_agenda']:
            raise ValueError('campaign_evolution_actor_unchanged')
        actor_updates.append({'actor_id': source['id'], 'current_state': state, 'current_agenda': agenda})
    nodes = {r['key']: r for r in snapshot['stage']['nodes']
             if snapshot['progress'][r['key']]['result'] == 'pending'
             and snapshot['progress'][r['key']]['knowledge'] != 'hidden'}
    task_updates, seen = [], set()
    for row in _array(raw, 'task_updates', len(nodes)):
        key = row.get('node_key')
        if not isinstance(key, str) or key not in nodes or key in seen:
            raise ValueError('campaign_evolution_task_authority_invalid')
        seen.add(key)
        title, objective = _text(row.get('title'), 'title', 120), _text(row.get('objective'), 'objective', 1200)
        if (title, objective) == (nodes[key]['title'], nodes[key]['objective']):
            raise ValueError('campaign_evolution_task_unchanged')
        task_updates.append({'node_key': key, 'title': title, 'objective': objective})
    notice = raw.get('public_notice')
    if 'public_notice' not in raw:
        raise ValueError('campaign_evolution_notice_missing')
    if notice is not None:
        if not isinstance(notice, dict):
            raise ValueError('campaign_evolution_notice_invalid')
        ids = notice.get('basis_log_ids')
        allowed = {r['log_id'] for r in snapshot['reports']}
        if (not isinstance(ids, list) or not ids or len(ids) > len(allowed)
                or any(type(i) is not int or i not in allowed for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError('campaign_evolution_notice_source_invalid')
        notice = {'title': _text(notice.get('title'), 'notice.title', 120),
                  'summary': _text(notice.get('summary'), 'notice.summary', 1200), 'basis_log_ids': sorted(ids)}
    if disposition == 'quiet' and (updates or location_updates or actor_updates or task_updates or notice):
        raise ValueError('campaign_evolution_quiet_has_changes')
    if disposition == 'changed' and not updates:
        raise ValueError('campaign_evolution_changed_missing_front')
    if actor_updates and not changed_maps:
        raise ValueError('campaign_evolution_actor_without_world_change')
    if task_updates and notice is None:
        raise ValueError('campaign_evolution_tasks_require_public_notice')
    return {'input_hash': snapshot['input_hash'], 'disposition': disposition,
            'reason': _text(raw.get('reason'), 'reason'), 'front_updates': updates,
            'location_updates': location_updates, 'actor_updates': actor_updates,
            'task_updates': task_updates, 'public_notice': notice}


def normalize_review(raw, snapshot, proposal):
    if (not isinstance(raw, dict) or raw.get('input_hash') != snapshot['input_hash']
            or raw.get('proposal_hash') != digest(proposal)):
        raise ValueError('campaign_evolution_review_identity_invalid')
    decision = raw.get('decision')
    if not isinstance(decision, str) or decision not in ('allow', 'reject'):
        raise ValueError('campaign_evolution_review_decision_invalid')
    return {'input_hash': snapshot['input_hash'], 'proposal_hash': digest(proposal),
            'decision': decision, 'reason': _text(raw.get('reason'), 'review.reason')}


WORLD_RULE = '''
职责是本区共同战役在没有新战报期间的世界自主行动。冻结 source / input_hash / 当前阶段实例、前线、人物、地点现状、最近各地已发布战报和 UTC 时钟为唯一依据。stage 仅是当前阶段，不提供未来路线；maps/scopes 和历史人物关联不单独证明人物当前在场、知情或仍能行动。
先判定存续主体的既定目标、当前能力、资源、路线和行动条件是否持续成立，再在 elapsed_seconds 内推演一次有限的行动与实际后果。经过时间只证明可用时长；必要条件被阻断或证据不足时保持现状，输出 quiet，不补造条件。不为达到固定频次强行恶化，不补算过去每一天的多波行动。适用的 action_window 只按真实起点和条件核对，不把阈值当作已发生结果。
每项变化给出完整新当前态，保留一切未受影响的既有事实、损失、伤势、俘获、死亡、救援和被切断的供能/物流；前线 summary 应同时承接对应 location_updates / actor_updates。仅现有存续主体可行动，不创造新人物、援军、资产、能力或新据点，不解除封印、复活或自动恢复已消灭力量。人物 current_state/current_agenda 保留状态、身份、实力和所在作用域，不跨图迁移；write_available=false 的人物正在被未结束探险使用，不能参与本轮新的过程，也不可改写。人物/地点的 state_updated_at 是记录保存时间，不单独证明过程起点；战役闲置时长不能当成后来新条件已经持续同样时长，只有冻结事实明确支持的持续条件才使用其相应时段，否则保持未证实的结果。本轮未更新地点的实际变化不可只塞入前线 summary。普通地图继续可探索，局部阻碍必须有因果和可继续调查/应对的空间。一次性活动内容只在其冻结活动地图内成立，不迁入旧副本的人物、势力、知识或地名。
front_updates 只修改可写 dungeon_front 的完整当前 summary，地点仅修改精确 location_id / location_name / expected_revision 的 new_state，人物仅修改 active 记录的现状与近期目标。现状中的死亡或被俘同样约束行动；active 枚举不推翻这些事实。若具体地点没有持久 ID，保持其变化待核验，不写成已经发生。location_updates 的 change_summary 解释具体变化及其持续有效的因果。所有现状可为后续现场探索提供客观输入，不自动成为玩家或酒馆知识。
public_notice 可为 null；非空时仅引用 basis_log_ids 中真实已公开的知识和这次客观时间间隔，描述可传播的既知局势、消息空缺或待核验风险。不创造传讯者/目击或把本次后台敌方行动直接公告，不透露未揭示地图、幕后身份、未来目标、弱点和解法。公开新事实仍待真实探险发布；后台地点更新不改 public_risk_notice。
task_updates 只可修改当前已公开且 pending 节点的 title/objective，以已知事实调整行动说明，必须同时有对应合法 public_notice；保留地图、原始成功/失败条件、挑战、窗口、必做属性、进度和历史证据。不换任务、不降低目标、不替玩家确认成功/失败/发现或推进阶段。已完成和隐藏节点不可改写。旧目标若因世界行动永久无法达成，必须已有准确适用的 failure_condition 且 stage.has_failure_exit=true，保留由真实探险核验失败的机会；否则该破坏过程不在本轮写权限内，保持目标所需条件并选择其它有据行动或 quiet。临时阻碍须仍能以原成功条件完成目标，不能制造永远 pending 的必做任务。
候选 proposal 的完整字段为 input_hash、disposition、reason、front_updates、location_updates、actor_updates、task_updates、public_notice，生成方输出该 proposal；独立审核方只评判它并使用自己的判决格式。disposition 仅 changed / quiet；changed 至少一项真实 front_updates，quiet 所有数组为空且 public_notice=null。backend limits 给出每个文本字段最大字符数及各数组最大记录数。各数组按 thread_id / location_id / actor_id / node_key 去重，basis_log_ids 按日志 ID 去重；所有身份只选冻结白名单。超出上限或重复身份整份拒收，不截断文本或丢弃独立记录。未使用的字段不取得写权限。
抽象格式成品（占位值仅示意，实际 ID/hash/内容全部取冻结输入）：
{"input_hash":"冻结哈希","disposition":"quiet","reason":"必要条件未成立的依据","front_updates":[],"location_updates":[],"actor_updates":[],"task_updates":[],"public_notice":null}
{"input_hash":"冻结哈希","disposition":"changed","reason":"仍成立的条件及本轮后果","front_updates":[{"thread_id":1,"summary":"完整现状"}],"location_updates":[{"location_id":2,"location_name":"冻结地点名","expected_revision":3,"new_state":"完整地点现状","change_summary":"有据变化"}],"actor_updates":[{"actor_id":4,"current_state":"完整人物现状","current_agenda":"近期目标"}],"task_updates":[{"node_key":"冻结节点键","title":"已知范围内标题","objective":"保留原目标的行动说明"}],"public_notice":{"title":"公开标题","summary":"既知信息与待核验风险","basis_log_ids":[5]}}
非法组合：quiet 携带任何写入；changed 没有前线变化；已结束前线或非 active 人物写入；未冻结/跨区地点 ID；隐藏/已完成节点改写；时间单独证明援军或死亡；公告泄露私有变化。遇到条件不足保持 quiet，不补写自称证据。
'''.strip()

GENERATION_RULE = '根据冻结事实提出一次世界自主行动候选，只输出完整 JSON。\n' + WORLD_RULE
REVIEW_RULE = ('独立核对同一冻结事实与规范化 proposal，不补造依据、不修订提案。\n' + WORLD_RULE + '''
核对每项因果前提、状态继承、地点/人物/前线一致性、知识边界与原任务目标。全部满足才 allow；任何实际矛盾、未知前提被补造、遗漏地点变更、已取得战果被撤销或公开越界则 reject。quiet 是合法结果，不因没有恶化而拒绝。
只输出 input_hash、proposal_hash、decision、reason；decision 仅 allow/reject，proposal_hash 必须与输入完全相同。allow/reject 都有真实非空理由，不能借允许判决增加提案写权限。
合法成品：{"input_hash":"冻结哈希","proposal_hash":"提案哈希","decision":"allow","reason":"全部条件有冻结依据且变化一致"}
合法拒绝：{"input_hash":"冻结哈希","proposal_hash":"提案哈希","decision":"reject","reason":"指出实际未成立条件或矛盾"}
非法：decision 为其它值、缺少任一 hash、用自行修订的提案换取 allow。
''').strip()


def input_messages(snapshot, proposal=None):
    # Neither the whole future plan nor historical story bodies enter this job.
    payload = deepcopy(snapshot)
    payload['limits'] = {
        'text_chars': {'reason': int(config('TEXT_LIMIT')), 'front_updates.summary': int(config('TEXT_LIMIT')),
            'actor_updates.current_state': int(config('TEXT_LIMIT')), 'actor_updates.current_agenda': int(config('TEXT_LIMIT')),
            'location_updates.new_state': LOCATION_STATE_TEXT_MAX_LENGTH,
            'location_updates.change_summary': LOCATION_CHANGE_SUMMARY_MAX_LENGTH,
            'task_updates.title': 120, 'task_updates.objective': 1200,
            'public_notice.title': 120, 'public_notice.summary': 1200, 'review.reason': int(config('TEXT_LIMIT'))},
        'array_records': {
            'front_updates': sum(r['campaign_role'] == 'dungeon_front' and r['status'] in (
                'seeded', 'active', 'escalated', 'closing') for r in snapshot['fronts']),
            'location_updates': len(snapshot['world']['locations']),
            'actor_updates': sum(r['status'] == 'active' and r['write_available'] for r in snapshot['world']['actors']),
            'task_updates': sum(r['result'] == 'pending' and r['knowledge'] != 'hidden' for r in snapshot['progress'].values()),
            'public_notice.basis_log_ids': len({r['log_id'] for r in snapshot['reports']})},
    }
    if proposal is not None:
        payload.update(proposal=proposal, proposal_hash=digest(proposal))
    return [{'role': 'system', 'content': REVIEW_RULE if proposal is not None else GENERATION_RULE},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}]
