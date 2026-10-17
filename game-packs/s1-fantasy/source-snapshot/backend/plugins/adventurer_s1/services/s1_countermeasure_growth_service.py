"""Post-encounter learned capability proposals, review and versioned application."""
from copy import deepcopy

from .s1_capability_profile_contract import normalize_actor_capability_profile
from .s1_encounter_preparation_contract import digest


GROWTH_OUTPUT_RULE = """学习也能形成新的本领。为所选能力缺口先确定：成长生效时，原先哪个导致目标失败或行动受阻的结果会由能完成变为不能完成。随后创造实现这一改变所需、与主体相容的实际功能，写清作用条件、覆盖范围及条件成立后的确定后果。仅改善旁支而使所选失败结果仍原样完成，表示该缺口尚未补足。capabilities 交付生效之后会发生什么；下次交锋负责判断双方功能是否实际生效，不替本次补造能力效果。保留未受新功能影响的其它作用，客观限制沿实际范围、条件和余量表述，未来胜负仍留给真实交锋。成长发生在本次遭遇之后，不要求旧正文已经写出新能力，也不倒改旧战斗；未知原理和远端信息保持未知，但不妨碍针对已观察作用设计新的本领。对方的瞬时或无条件表述不自动高于主体的新本领，双方仍按实际作用相交。

capability_growth 数组记录本次能力成长，每个主体至多一项，含 observer_world_actor_id、review_indexes、reason、capability_profile、power_boundary。review_indexes 是该主体本次 problems 中支撑成长的准确 review_index 非空数组；reason 简述反馈如何促成已完成的战后适应。capability_profile 沿 actors 中同名字段的完整结构返回成长后的画像，保留仍有效的全部原本领，改变的功能与原限制同步相容；power_boundary 是与新画像一致的简短总体边界。只在输入提供完整画像时提交能力成长；没有成长时写 []。countermeasure_direction 只记当前既有本领可实施的战术，新增功能统一写入 capability_growth，核对与保存成功后才成为后续遭遇的本领。"""

GROWTH_REVIEW_PROMPT = """你核对长期主体由遭遇经验形成的战后能力成长。actors 是成长前完整档案，problems 是已核对的可知反馈，final_events 仅用于核对主体是否存续及时间边界，proposals 是成长提案。problems 中 recognized_method 描述冒险者施用的手段，learning_feedback 是绑定主体观察或承受该作用的反馈。先核对施用者、受作用者与变化归属，再核对提案如何从该反馈形成新本领；把主体自身受创当作另一方已有伤势或弱点的依据不成立。由自身受创发展新的进攻本领仍可成立，但其实际功能和适用条件须独立完整。
成长允许增加实际本领，不要求旧正文预先写出新能力。按提案声称弥补的能力缺口核对：先按对方手段正常成立确定待改变的结果，再检查新本领能否在所需时机与范围内作用于该结果的成立环节，以及作用成立后哪些条件实际改变、原结果是否因此改变。只说明会介入或产生扰动，却缺少从实际功能到所声称改变的关系，不通过该成长提案；明确的局部改善按其实际目标核对，不额外要求彻底克制。只有保持行动、提高意愿或重复原本领，却不能接触或改变所声称应对的作用，不构成该项能力缺口的成长。review_indexes 绑定促成成长的反馈来源，不表示成长必须克制所有来源机制；未声称覆盖的机制不据此否定其他有依据的成长。也须与主体身份和连续性相容，完整画像保留未被改变的原本领、同步处理相交的限制。对方正常成立的瞬时或无条件作用也参与比较，不能仅靠更强的名称或档位认定应对成立。提案不能补造过去的知情、反转死亡、夺取他方资产或改写已经结束的战斗；成长不等于未来必胜。已死亡或不再具备学习主体资格的角色不通过。
只输出 reviews 数组，每个提案主体恰好一项，含 observer_world_actor_id、approved 布尔值、reason 非空字符串。approved=true 表示该完整成长提案通过，false 表示保留原能力；不改提案、不补新能力，不评价伤亡人数。"""


def attach_proposals(value, experiences, reviewed, learning_input):
    """Keep growth outside the knowledge contract; project every nested profile."""
    raw = value.get('capability_growth', [])
    if not isinstance(raw, list):
        return [{'reason': 'learning_growth_array_required'}]
    actors = {a['id']: a for a in learning_input['actors']}
    by_index = {row['review_index']: row for row in experiences}
    counts = {}
    for row in raw:
        aid = row.get('observer_world_actor_id') if isinstance(row, dict) else None
        if type(aid) is int:
            counts[aid] = counts.get(aid, 0) + 1
    errors = []
    for row in raw:
        aid = row.get('observer_world_actor_id') if isinstance(row, dict) else None
        actor = actors.get(aid) if type(aid) is int else None
        indexes = row.get('review_indexes') if isinstance(row, dict) else None
        base = normalize_actor_capability_profile(actor.get('capability_profile')) if actor else None
        proposed = normalize_actor_capability_profile(row.get('capability_profile')) if isinstance(row, dict) else None
        reason = row.get('reason') if isinstance(row, dict) else None
        boundary = row.get('power_boundary') if isinstance(row, dict) else None
        if (not actor or counts.get(aid) != 1 or base is None or proposed is None
                or type(actor.get('capability_revision')) is not int or actor['capability_revision'] <= 0
                or not isinstance(indexes, list) or not indexes
                or any(type(i) is not int or i not in by_index
                       or by_index[i]['observer_world_actor_id'] != aid for i in indexes)
                or len(set(indexes)) != len(indexes)
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(boundary, str) or not boundary.strip()):
            errors.append({'reason': 'learning_growth_proposal_invalid', 'observer_world_actor_id': aid})
            continue
        if base == proposed:
            continue
        by_index[indexes[0]]['_capability_growth'] = {
            'observer_world_actor_id': aid, 'base_revision': actor['capability_revision'],
            'base_profile': base, 'capability_profile': proposed,
            'base_power_boundary': actor.get('power_boundary'), 'power_boundary': boundary.strip(),
            'review_indexes': sorted(indexes), 'reason': reason.strip(),
        }
    return errors


def normalize_reviews(value, proposals):
    expected = {p['observer_world_actor_id'] for p in proposals}
    rows = value.get('reviews') if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError('learning_growth_reviews_required')
    result = {}
    for row in rows:
        aid = row.get('observer_world_actor_id') if isinstance(row, dict) else None
        if (type(aid) is not int or aid not in expected or aid in result
                or type(row.get('approved')) is not bool
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('learning_growth_review_invalid')
        result[aid] = {'observer_world_actor_id': aid, 'approved': row['approved'],
                       'reason': row['reason'].strip()}
    if set(result) != expected:
        raise ValueError('learning_growth_review_scope_incomplete')
    return list(result.values())


def review_growth(learning_input, reviewed, experiences, call_args):
    from . import s1_countermeasure_learning_ai_service as L
    from .s1_countermeasure_review_design_service import build_design_view, seal_review_sources
    proposals = [deepcopy(r['_capability_growth']) for r in experiences if '_capability_growth' in r]
    if not proposals:
        return {'status': 'not_applicable', 'approved': []}
    for proposal in proposals:
        support = [r for r in experiences if r['review_index'] in proposal['review_indexes']]
        records, errors = seal_review_sources(reviewed, support, learning_input)
        if errors or len(records) != len(proposal['review_indexes']):
            return {'status': 'deferred', 'approved': [], 'error_code': 'learning_growth_support_invalid'}
        proposal['supporting_records'] = records
    payload = build_design_view(learning_input, reviewed)
    payload.update(proposals=[{k: v for k, v in p.items() if k != 'supporting_records'} for p in proposals],
                   final_events=deepcopy(learning_input['final_events']))
    normalized, error, metadata = L._call_phase(
        'growth_review', payload, lambda v: normalize_reviews(v, proposals), **call_args)
    if error:
        return {'status': 'deferred', 'approved': [], 'error_code': error, 'metadata': metadata}
    allowed = {r['observer_world_actor_id'] for r in normalized if r['approved']}
    approved = [p for p in proposals if p['observer_world_actor_id'] in allowed]
    sealed = {'approved': approved, 'reviews': normalized,
              'source_log_id': call_args['expedition_log_id']}
    return {'status': 'reviewed', **sealed, 'fingerprint': digest(sealed), 'metadata': metadata}


def valid_receipt(growth):
    return (isinstance(growth, dict) and growth.get('status') == 'reviewed'
            and isinstance(growth.get('approved'), list)
            and isinstance(growth.get('reviews'), list)
            and growth.get('fingerprint') == digest({k: growth.get(k) for k in (
                'approved', 'reviews', 'source_log_id')}))


def support_key(row):
    """Bind growth to exact personal records consumed in this transaction."""
    fields = ('observer_world_actor_id', 'observer_capability_revision', 'matched_knowledge_id',
              'source_refs', 'encountered_method', 'observed_effect', 'unresolved_reason',
              'learned_boundary', 'knowledge_level', 'countermeasure_direction', 'evidence_event_indexes')
    return digest({k: row.get(k) for k in fields})


def persist_growth(receipt, *, expedition_log_id, persisted_support_keys):
    """Call only inside the same transaction that saved the sealed learning."""
    from plugins.adventurer_s1.models import AdventurerS1WorldActor as Actor
    growth = receipt.get('capability_growth') if isinstance(receipt, dict) else None
    if not isinstance(growth, dict) or growth.get('status') != 'reviewed':
        return {'updated': [], 'deferred': []}
    proposals = growth.get('approved')
    if not valid_receipt(growth) or growth.get('source_log_id') != expedition_log_id:
        raise ValueError('learning_growth_receipt_changed')
    ids = [p['observer_world_actor_id'] for p in proposals]
    approved_ids = {r['observer_world_actor_id'] for r in growth['reviews'] if r.get('approved') is True}
    if len(set(ids)) != len(ids) or set(ids) != approved_ids:
        raise ValueError('learning_growth_duplicate_actor')
    actors = {a.id: a for a in Actor.query.filter(Actor.id.in_(ids)).order_by(Actor.id)
              .with_for_update().populate_existing().all()} if ids else {}
    result = {'updated': [], 'deferred': []}
    for proposal in proposals:
        aid = proposal['observer_world_actor_id']; actor = actors.get(aid)
        base = proposal['base_profile']; target = proposal['capability_profile']
        revision = proposal['base_revision']
        if (actor and actor.capability_revision == revision + 1
                and actor.capability_source_log_id == expedition_log_id
                and actor.capability_profile == target):
            continue
        support = proposal.get('supporting_records') or []
        if (not support or any(support_key(r) not in persisted_support_keys for r in support)
                or not actor or actor.status != 'active'
                or actor.capability_revision != revision or actor.capability_profile != base
                or (actor.power_boundary or '') != (proposal.get('base_power_boundary') or '')):
            result['deferred'].append({'actor_id': aid, 'reason': 'learning_growth_base_changed_or_ineligible'})
            continue
        if normalize_actor_capability_profile(target) != target:
            raise ValueError('learning_growth_profile_invalid')
        actor.capability_profile = deepcopy(target)
        actor.power_boundary = proposal['power_boundary']
        actor.capability_revision = revision + 1
        actor.capability_source_log_id = expedition_log_id
        result['updated'].append({'actor_id': aid, 'capability_revision': revision + 1})
    return result
