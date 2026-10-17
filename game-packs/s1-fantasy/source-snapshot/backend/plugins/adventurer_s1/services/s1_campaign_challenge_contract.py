"""Operator-authored local challenge facts, frozen for one campaign task."""
from copy import deepcopy

from flask import current_app

from plugins.adventurer_s1.services.s1_capability_profile_contract import (
    CAPABILITY_AXIS_KEYS, normalize_actor_capability_profile,
)


def _limit(name):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_CAMPAIGN_CHALLENGE_' + name
    return int(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or len(value.strip()) > _limit('MAX_TEXT') or (not empty and not value.strip()):
        raise ValueError(f'挑战配置 {name} 文本缺失或过长')
    return value.strip()


def _level(value):
    if type(value) is not int or not 1 <= value <= _limit('MAX_LEVEL'):
        raise ValueError('挑战配置等级超出允许范围')
    return value


def normalize_challenge(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('challenge 必须为对象或 null')
    result = {'recommended_level': _level(value.get('recommended_level'))}
    for key in ('public_brief', 'battlefield', 'preparation_effects'):
        result[key] = _text(value.get(key), key, empty=key == 'preparation_effects')
    opponents = value.get('opponents')
    if not isinstance(opponents, list) or not 1 <= len(opponents) <= _limit('MAX_OPPONENTS'):
        raise ValueError('挑战配置必须包含有界的敌方能力档案')
    result['opponents'] = []
    subjects = set()
    for raw in opponents:
        if not isinstance(raw, dict):
            raise ValueError('敌方能力档案必须为对象')
        subject = _text(raw.get('subject'), 'subject')
        scope = raw.get('scope')
        if subject in subjects or not isinstance(scope, str) or scope not in ('individual', 'group'):
            raise ValueError('敌方名称必须唯一，scope 必须为 individual 或 group')
        subjects.add(subject)
        _level(raw.get('basis_level'))
        # Shape checks precede the shared normalizer; no text is interpreted as a capability.
        for key in ('typical_profile', 'current_profile'):
            axes = raw.get(key)
            if not isinstance(axes, dict) or any(not isinstance(axes.get(axis), str) for axis in CAPABILITY_AXIS_KEYS):
                raise ValueError('敌方能力维度必须完整')
        if any(not isinstance(raw.get(key), str) for key in ('condition', 'number_pressure')):
            raise ValueError('敌方状态与数量压力必须完整')
        for key in ('capabilities', 'limitations'):
            items = raw.get(key)
            if not isinstance(items, list) or not 1 <= len(items) <= _limit('MAX_TRAITS'):
                raise ValueError('敌方能力与限制必须为非空有界数组')
            for item in items:
                _text(item, key)
        profile = normalize_actor_capability_profile(raw)
        if profile is None:
            raise ValueError('敌方能力档案不符合现行能力合同')
        result['opponents'].append({'subject': subject, 'scope': scope, **profile})
    return result


def project_challenge_context(value):
    if not isinstance(value, dict) or not isinstance(value.get('challenge'), dict):
        raise ValueError('冻结挑战上下文缺失')
    challenge = normalize_challenge(value['challenge'])
    reports = value.get('prior_reports')
    if not isinstance(reports, list):
        raise ValueError('冻结挑战缺少已发布战报范围')
    projected = []
    for row in reports:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                                            for k in ('dungeon', 'title', 'summary')):
            raise ValueError('冻结挑战的已发布战报不完整')
        projected.append({k: row[k] for k in ('dungeon', 'title', 'summary')})
    return {'challenge': challenge, 'prior_reports': projected}


def freeze_challenge_context(context, node, published):
    if context['discovery'] is True or node.get('challenge') is None:
        return
    context['challenge_context'] = project_challenge_context({
        'challenge': node['challenge'],
        'prior_reports': [{'dungeon': name, 'title': row['public_title'], 'summary': row['public_summary']}
                          for name, row in sorted(published.items())],
    })


def public_challenge(node):
    value = node.get('challenge')
    return ({'recommended_level': value['recommended_level'], 'brief': value['public_brief']}
            if value else None)


def update_future_challenges(plan, patches, entered_stage_keys):
    """Change only never-entered stage nodes; preserve the rest of a legacy plan verbatim."""
    if not isinstance(patches, list) or not patches:
        raise ValueError('请提供待调整的阶段任务挑战配置')
    nodes = {(stage['key'], node['key']): node for stage in plan['stages'] for node in stage['nodes']}
    if len(patches) > len(nodes):
        raise ValueError('挑战调整数量超过阶段任务数')
    updated = deepcopy(plan)
    targets = {(stage['key'], node['key']): node for stage in updated['stages'] for node in stage['nodes']}
    changes, seen = [], set()
    for patch in patches:
        if not isinstance(patch, dict) or any(not isinstance(patch.get(k), str) for k in ('stage_key', 'node_key')):
            raise ValueError('挑战调整必须引用准确阶段与节点标识')
        identity = (patch['stage_key'], patch['node_key'])
        if identity not in nodes or identity in seen or 'challenge' not in patch:
            raise ValueError('挑战调整的节点不存在、重复或缺少配置')
        if identity[0] in entered_stage_keys:
            raise ValueError('已经开启过的阶段不能调整挑战配置')
        seen.add(identity)
        value = normalize_challenge(patch['challenge'])
        old = nodes[identity].get('challenge')
        if value == old:
            continue
        changes.append({'stage_key': identity[0], 'node_key': identity[1],
                        'before': deepcopy(old), 'after': value})
        if value is None:
            targets[identity].pop('challenge', None)
        else:
            targets[identity]['challenge'] = value
    if not changes:
        raise ValueError('挑战配置没有变化')
    return updated, changes


CHALLENGE_GENERATION_RULE = '''
冻结 challenge_context.challenge 是本次正式地图任务的局部挑战设定。opponents 与现行 encounter_capability_profiles 使用相同的主体/群体、基准等级、五维画像、状态、数量压力及能力与限制语义。battlefield 建立本地防守与环境关系，preparation_effects 说明既有行动如何改变它们，只有 prior_reports、冻结当前态或本场事件确已建立对应前提时才应用这些影响；未成立的准备不能自动兑现。
本配置为尚未确立能力的本地战役敌方提供明确基准，可高于地图常规推荐等级，优先于后台泛化的挑战描述；它不提高整张地图的普通生态，也不按本次队伍强弱重新缩放。已经确立的精确主体能力版本、生死、伤势、据点毁损与资源去向继续作为当前事实，不能用配置恢复或加码。客观大纲只补充与这些边界兼容的局势，主剧情从实际行动、覆盖范围和仍存在的压力形成结果。recommended_level 与 public_brief 是接取提示，不是伤害倍率、胜率、生死或完成次数要求。对仍在行动且具有作用条件的敌方与机制，按其既有目标实际推进局势；角色的有效准备与应对照实改变结果，不能仅因进入决战就预定失败、拖延或成功。敌方档案和准备条件不自动成为角色知识；角色按实际观察与可信获知形成认识，具体内幕仍服从本场揭示范围。
'''.strip()

CHALLENGE_OBSERVER_RULE = '''
冻结条目含 challenge_context 时，按同一局部挑战设定与更高优先级的既成世界事实核对正文实际采用的敌方能力、防守关系和前期成果。若正文无因果削弱或提高既定能力、恢复已经覆灭的主体或防线、凭未发生的准备消除关键压力，且已经改变核心行动或结果、无法通过公告或非生命结算补丁修复，使用现有 needs_regeneration=true、regeneration_basis=other_unrepairable，并用 regeneration_evidence.main_event_indexes 指向对应正文事件。保留有据的胜利、撤退、失败与实际削弱；不得以一场完成、全员生还、伤害偏小或缺少某种战术为由打回，也不补写预定难度、伤亡或出场次数。
'''.strip()
