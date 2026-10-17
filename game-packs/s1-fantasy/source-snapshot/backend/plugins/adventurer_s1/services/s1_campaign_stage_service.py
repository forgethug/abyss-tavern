"""Automatic or manual release using the same published-evidence gates."""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from uuid import uuid4

from flask import current_app

from db_repository import db_manager
from db_repository.exceptions import DatabaseError, TransactionError
from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository as Repo
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService as Snapshots
from plugins.adventurer_s1.services.s1_campaign_stage_contract import (
    RESULTS, ADVANCE_MODES, advance_mode, stage_definition, initial_progress,
    eligible_branches, available_stage_maps,
    normalize_completion_nodes, completion_node_keys,
)
from plugins.adventurer_s1.services.s1_realm_service import S1RealmService
from plugins.adventurer_s1.services.s1_campaign_revelation_contract import (
    public_stage_title, update_unrevealed_revelations,
)
from plugins.adventurer_s1.services.s1_campaign_challenge_contract import update_future_challenges
from plugins.adventurer_s1.services.s1_campaign_ending_service import ending_proposal, ending_snapshot

OPERATION = 'campaign_stage_operation_v1'


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _identity():
    source = Repo.identity()
    realm = S1RealmService.current_runtime_realm()
    key = 'S1_RUNTIME_DEPLOYMENT_ID' if realm == 'region_2' else 'ADVENTURER_S1_REGION_1_DEPLOYMENT_ID'
    if (not source or source['realm_code'] != realm or not source['deployment_id']
            or source['deployment_id'] != current_app.config.get(key)):
        raise ValueError('战役数据库区服或部署身份不匹配')
    return source


def _snapshot(dispatch, state, fronts, source, *, include_ending=False):
    return {
        'source': source, 'campaign_root_id': dispatch.campaign_root_id, 'dispatch_id': dispatch.id,
        'revision': dispatch.revision, 'stage_key': state.stage_key, 'stage_instance': state.stage_instance,
        'plan_hash': _hash(state.plan), 'progress': deepcopy(state.progress), 'status': state.status,
        'advance_mode': advance_mode(state.plan),
        'objective': dispatch.objective, 'target_dungeons': list(dispatch.target_dungeons or []),
        'front_versions': sorted([[f.id, f.revision, f.status, f.is_chain_head] for f in fronts]),
        **(ending_snapshot(dispatch) if include_ending else {}),
    }


def _available_maps(fronts):
    return {f.base_dungeon for f in fronts if f.campaign_role == 'dungeon_front'
            and f.is_chain_head is not False and f.status in ('seeded', 'active', 'escalated', 'closing')}


def _front_recovery(dispatch, state, stage, fronts, request):
    thread_id = request.get('thread_id')
    if type(thread_id) is not int or thread_id < 1:
        raise ValueError('必须选择准确的待恢复前线 ID')
    if request.get('confirm_clock_only') is not True:
        raise ValueError('请先核验原始世界事实，确认前线仅被自动计数沉寂、没有真实终结')
    front = next((f for f in fronts if f.id == thread_id), None)
    if (not front or front.campaign_root_id != dispatch.campaign_root_id
            or front.campaign_role != 'dungeon_front' or front.is_chain_head is False
            or front.superseded_by_id is not None or front.status != 'dormant'):
        raise ValueError('只能恢复本战役当前仍为沉寂状态的地图前线，已收束或被后继替代的前线不能恢复')
    node = next((n for n in stage['nodes'] if n['dungeon'] == front.base_dungeon), None)
    if state.status != 'active' or not node or state.progress[node['key']]['result'] != 'pending':
        raise ValueError('只能恢复当前进行中阶段尚待完成的地图任务')
    # This is an explicit administrator attestation, never a semantic inference
    # from story text. Hash the exact current evidence again at execution time.
    return {
        'thread_id': front.id, 'node_key': node['key'], 'dungeon': front.base_dungeon,
        'title': front.title, 'confirm_clock_only': True,
        'before': {'status': front.status, 'revision': int(front.revision or 1),
                   'closure_debt': int(front.closure_debt or 0)},
        'after': {'status': 'active', 'revision': int(front.revision or 1) + 1, 'closure_debt': 0},
        'evidence': {
            'summary': front.summary, 'next_reveal_hint': front.next_reveal_hint,
            'power_profile': front.power_profile, 'campaign_agenda': front.campaign_agenda,
            'scope_keys': deepcopy(front.scope_keys), 'chain_key': front.chain_key,
            'predecessor_id': front.predecessor_id,
            'last_progress_log_id': front.last_progress_log_id,
            'last_seen_log_id': front.last_seen_log_id,
            'source_candidate_ids': deepcopy(front.source_candidate_ids),
        },
    }


def _evidence(dispatch, state, node_key, log_id, rows, *, terminal=False):
    if type(log_id) is not int or log_id < 1:
        raise ValueError('必须引用已经发布的准确战报日志 ID')
    evidence = rows.get(log_id)
    if not evidence:
        raise ValueError('战报尚未发布或探险未完成，不能作为阶段证据')
    report, log = evidence
    context = (log.full_log or {}).get('campaign_dispatch') or {}
    task = context.get('stage_task') or {}
    task_revision = report.revision
    if context.get('parallel_sites') is True:
        task_revision = (report.report or {}).get('dispatch_revision')
        if type(task_revision) is not int or task_revision < 1:
            raise ValueError('并行战报缺少准确的后台任务版本证据')
    if ((task.get('stage_key'), task.get('stage_instance'), task.get('node_key')) !=
            (state.stage_key, state.stage_instance, node_key)
            or context.get('dispatch_id') != dispatch.id or context.get('revision') != task_revision):
        raise ValueError('证据不属于当前阶段实例的这项地图任务')
    if terminal and context.get('discovery') is not False:
        raise ValueError('首次发现战报只能开放调查，不能作为正式任务完成或失败的证据')
    return report, log


def _proposal(dispatch, state, fronts, kind, request):
    if kind == 'ending_summary':
        return ending_proposal(dispatch, state, fronts, request)
    if dispatch.ended_at or state.status == 'ended' or (kind not in (
            'advance_mode', 'challenge_update', 'revelation_update') and not dispatch.discovered_at):
        raise ValueError('战役尚未发现或已经结束')
    if dispatch.active_log_id or Repo.busy([f.id for f in fronts]):
        raise ValueError('仍有小队执行或世界事实待处理，请等待战报发布')
    stage = stage_definition(state.plan, state.stage_key)
    result = {'stage_key': state.stage_key, 'stage_instance': state.stage_instance,
              'progress': deepcopy(state.progress), 'status': state.status,
              'advance_mode': advance_mode(state.plan)}
    if kind == 'transition':
        branch = request.get('branch')
        if branch not in eligible_branches(stage, state.progress):
            raise ValueError('阶段条件尚未满足，不能开放所选分支')
        # Every terminal result, including a manual correction, retains a published
        # report from this exact instance. Hints alone cannot satisfy a gate.
        rows = Repo.evidences(dispatch.id, {e['evidence_log_id'] for e in state.progress.values()
                                            if e['result'] != 'pending'})
        evidence_hashes = {}
        for key, entry in state.progress.items():
            if entry['result'] != 'pending':
                report, log = _evidence(dispatch, state, key, entry['evidence_log_id'], rows, terminal=True)
                evidence_hashes[key] = _hash({'report': report.report, 'events': (log.full_log or {}).get('events')})
        result['evidence_hashes'] = evidence_hashes
        target = stage[branch + '_next']
        if target:
            next_stage = stage_definition(state.plan, target)
            if not {n['dungeon'] for n in next_stage['nodes']}.issubset(_available_maps(fronts)):
                raise ValueError('下一阶段缺少可推进的地图前线，请先检查世界事实')
            result.update(stage_key=target, stage_instance=state.stage_instance + 1,
                          progress=initial_progress(next_stage, revealed=True), status='active')
        else:
            result['status'] = 'ended'
        result['branch'] = branch
    elif kind == 'advance_mode':
        mode = request.get('advance_mode')
        if not isinstance(mode, str) or mode not in ADVANCE_MODES:
            raise ValueError('推进方式必须为 auto 或 manual')
        if mode == advance_mode(state.plan):
            raise ValueError('当前已使用该推进方式')
        result['advance_mode'] = mode
        result.update(target_dungeons=list(dispatch.target_dungeons or []), objective=dispatch.objective)
        return result
    elif kind == 'objectives_update':
        keys = normalize_completion_nodes(request.get('completion_nodes'), stage['nodes'])
        directive = request.get('continuation_directive', state.plan.get('continuation_directive', ''))
        if not isinstance(directive, str) or len(directive.strip()) > 1200:
            raise ValueError('后续编排要求必须为最多 1200 字的文本')
        plan = deepcopy(state.plan)
        updated_stage = stage_definition(plan, state.stage_key)
        updated_stage['completion_nodes'] = keys
        if updated_stage['failure_next'] is None and any(
                node['failure_condition'] for node in updated_stage['nodes'] if node['key'] in keys):
            raise ValueError('关键目标包含不可挽回的失败条件，必须先配置失败出口')
        plan['continuation_directive'] = directive.strip()
        if keys == completion_node_keys(stage) and directive.strip() == state.plan.get('continuation_directive', ''):
            raise ValueError('关键目标与后续编排要求没有变化')
        result.update(updated_plan=plan, objectives_changes={
            'before': {'completion_nodes': completion_node_keys(stage),
                       'continuation_directive': state.plan.get('continuation_directive', '')},
            'after': {'completion_nodes': keys, 'continuation_directive': directive.strip()},
        })
        result['status'] = 'ready' if eligible_branches(updated_stage, state.progress) else 'active'
    elif kind == 'challenge_update':
        entered = Repo.entered_stage_keys(dispatch.id, state.stage_key, state.plan['stages'][0]['key'])
        plan, changes = update_future_challenges(state.plan, request.get('challenges'), entered)
        result.update(updated_plan=plan, challenge_changes=changes,
                      target_dungeons=list(dispatch.target_dungeons or []), objective=dispatch.objective)
        return result
    elif kind == 'front_recovery':
        recovery = _front_recovery(dispatch, state, stage, fronts, request)
        active_maps = _available_maps(fronts) | {recovery['dungeon']}
        result.update(front_recovery=recovery, objective=dispatch.objective,
                      target_dungeons=[name for name in available_stage_maps(stage, state.progress)
                                       if name in active_maps])
        return result
    elif kind == 'revelation_update':
        if state.status != 'active':
            raise ValueError('请在进行中的阶段调整尚未揭示的线索')
        entered = Repo.entered_stage_keys(dispatch.id, state.stage_key, state.plan['stages'][0]['key'])
        plan, changes = update_unrevealed_revelations(
            state.plan, request.get('revelations'), state.stage_key, state.progress, entered)
        result.update(updated_plan=plan, revelation_changes=changes,
                      target_dungeons=list(dispatch.target_dungeons or []), objective=dispatch.objective)
        return result
    elif kind == 'progress_review':
        key, value = request.get('node_key'), request.get('result')
        node = next((n for n in stage['nodes'] if n['key'] == key), None)
        if not node or not isinstance(value, str) or value not in RESULTS:
            raise ValueError('地图任务或核验结果无效')
        if value == 'failed' and not node['failure_condition']:
            raise ValueError('本任务未配置不可挽回的失败条件')
        if value == 'pending' and node['dungeon'] not in _available_maps(fronts):
            raise ValueError('该地图前线已收束，不能恢复接取')
        log_id = request.get('evidence_log_id')
        if type(log_id) is not int or log_id < 1:
            raise ValueError('必须引用已经发布的准确战报日志 ID')
        report, log = _evidence(dispatch, state, key, log_id, Repo.evidences(dispatch.id, [log_id]),
                                terminal=value != 'pending')
        events = (log.full_log or {}).get('events')
        if not isinstance(events, list) or not events:
            raise ValueError('证据日志缺少完整正文，请先核查原始记录')
        result['progress'][key] = {
            'knowledge': 'discovered', 'result': value, 'evidence_log_id': log.id,
            'evidence_revision': report.revision, 'evidence_text': request['reason'],
        }
        result['status'] = 'ready' if eligible_branches(stage, result['progress']) else 'active'
        result['evidence'] = {'expedition_log_id': log.id, 'report': deepcopy(report.report),
                              'events': deepcopy(events), 'node': deepcopy(node)}
    else:
        raise ValueError('不支持的战役操作')
    target_stage = stage_definition(state.plan, result['stage_key'])
    active_maps = _available_maps(fronts)
    result['target_dungeons'] = ([name for name in available_stage_maps(target_stage, result['progress'])
                                  if name in active_maps]
                                 if result['status'] == 'active' else [])
    result['objective'] = '' if result['status'] == 'ended' else public_stage_title(state.plan, target_stage)
    return result


def _apply(dispatch, state, fronts, source, before, after, *, operation_key, kind, actor_id, reason):
    if kind == 'ending_summary':
        dispatch.public_title, dispatch.public_summary = after['public_title'], after['public_summary']
        dispatch.revision += 1
        dispatch.updated_at = _portable_utc_now()
        # Preserve ended_at and every gameplay field, including stage timestamps.
        return Repo.add_receipt(
            dispatch_id=dispatch.id, operation_key=operation_key, kind=kind,
            admin_user_id=actor_id, reason=reason, before=before,
            after={**_snapshot(dispatch, state, fronts, source, include_ending=True),
                   'evidence': after['evidence']})
    if kind in ('challenge_update', 'revelation_update', 'objectives_update'):
        state.plan = after['updated_plan']
    if kind == 'front_recovery':
        front = next(f for f in fronts if f.id == after['front_recovery']['thread_id'])
        Repo.restore_front(front)
    state.stage_key, state.stage_instance = after['stage_key'], after['stage_instance']
    state.progress, state.status = after['progress'], after['status']
    if after['advance_mode'] != advance_mode(state.plan):
        state.plan = {**deepcopy(state.plan), 'advance_mode': after['advance_mode']}
    dispatch.target_dungeons, dispatch.objective = after['target_dungeons'], after['objective']
    # These operations preserve current task content and already accepted tickets.
    if kind not in ('challenge_update', 'front_recovery', 'revelation_update'):
        dispatch.revision += 1
    dispatch.updated_at = _portable_utc_now()
    dispatch.ended_at = _portable_utc_now() if state.status == 'ended' else None
    Repo.touch(state)
    return Repo.add_receipt(
        dispatch_id=dispatch.id, operation_key=operation_key, kind=kind,
        admin_user_id=actor_id, reason=reason,
        before=before, after={**_snapshot(dispatch, state, fronts, source),
                              **({'challenge_changes': after['challenge_changes']} if kind == 'challenge_update' else {}),
                              **({'revelation_changes': after['revelation_changes']} if kind == 'revelation_update' else {}),
                              **({'objectives_changes': after['objectives_changes']} if kind == 'objectives_update' else {}),
                              **({'front_recovery': after['front_recovery']} if kind == 'front_recovery' else {})})


def _receipt(row, *, replayed):
    return {'receipt_id': row.id, 'operation_key': row.operation_key, 'kind': row.kind,
            'admin_user_id': row.admin_user_id, 'reason': row.reason,
            'before': row.before, 'after': row.after, 'replayed': replayed}


class S1CampaignStageService:
    @staticmethod
    def preview(root_id, kind, body, admin_user_id):
        try:
            if not isinstance(body, dict):
                raise ValueError('请求必须为对象')
            reason = body.get('reason')
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1200:
                raise ValueError('请填写核验理由，最多 1200 字')
            request = {k: body[k] for k in ('branch', 'node_key', 'result', 'evidence_log_id',
                                           'advance_mode', 'challenges', 'revelations', 'thread_id', 'confirm_clock_only',
                                           'public_title', 'public_summary', 'evidence_log_ids',
                                           'completion_nodes', 'continuation_directive') if k in body}
            request['reason'] = reason.strip()
            with db_manager.transaction():
                source = _identity()
                dispatch, state, fronts = Repo.bundle(root_id, lock=True)
                if not state:
                    raise ValueError('该战役没有阶段计划')
                if kind == 'transition' and state.plan.get('continuity_enabled') is True:
                    raise ValueError('此战役启用了阶段衔接，请先准备并执行衔接预览')
                before = _snapshot(dispatch, state, fronts, source, include_ending=kind == 'ending_summary')
                after = _proposal(dispatch, state, fronts, kind, request)
                payload = {'root_id': root_id, 'kind': kind, 'source': source,
                           'expected_hash': _hash(before), 'request': request,
                           'proposal_hash': _hash(after),
                           'operation_key': uuid4().hex}
            # No SQL connection / lock is retained while storing the preview.
            token = Snapshots.store(OPERATION, admin_user_id, payload, allow_signed_fallback=True)
            if not token:
                raise ValueError('无法保存操作预览，请稍后重试')
            return {'preview_token': token, 'before': before, 'after': after,
                    'reason': request['reason'], 'kind': kind}, None
        except (ValueError, DatabaseError, TransactionError) as exc:
            return None, _error(exc)

    @staticmethod
    def execute(root_id, kind, preview_token, admin_user_id):
        db_manager.remove()
        payload = Snapshots.load(OPERATION, admin_user_id, preview_token, allow_signed_fallback=True)
        if not payload or payload.get('root_id') != root_id or payload.get('kind') != kind:
            return None, '预览已失效或不属于本次操作，请重新核验'
        try:
            with db_manager.transaction():
                source = _identity()
                if payload['source'] != source:
                    raise ValueError('预览区服或部署已变化，请重新核验')
                dispatch, state, fronts = Repo.bundle(root_id, lock=True)
                if not state:
                    raise ValueError('阶段计划不存在')
                receipt = Repo.receipt(payload['operation_key'])
                if receipt:
                    if receipt.admin_user_id != admin_user_id or receipt.dispatch_id != dispatch.id or receipt.kind != kind:
                        raise ValueError('操作回执身份不匹配')
                    return _receipt(receipt, replayed=True), None
                before = _snapshot(dispatch, state, fronts, source, include_ending=kind == 'ending_summary')
                if _hash(before) != payload['expected_hash']:
                    raise ValueError('战役版本、进度或前线已变化，请重新预览')
                after = _proposal(dispatch, state, fronts, kind, payload['request'])
                if _hash(after) != payload['proposal_hash']:
                    raise ValueError('核验证据或操作结果已变化，请重新预览')
                receipt = _apply(dispatch, state, fronts, source, before, after,
                    operation_key=payload['operation_key'], kind=kind,
                    actor_id=admin_user_id, reason=payload['request']['reason'])
                return _receipt(receipt, replayed=False), None
        except (ValueError, DatabaseError, TransactionError) as exc:
            return None, _error(exc)

    @staticmethod
    def advance_automatic(root_id):
        """Own a short transaction; status and instance serialize competing workers."""
        try:
            with db_manager.transaction():
                source = _identity()
                dispatch, state, fronts = Repo.bundle(root_id, lock=True)
                if not state or advance_mode(state.plan) != 'auto' or state.status != 'ready':
                    return None, None
                if state.plan.get('continuity_enabled') is True:
                    from plugins.adventurer_s1.services.s1_campaign_continuation_service import _enqueue
                    _enqueue(dispatch, state, fronts, {'reason': '根据已发布战报自动核验阶段衔接'},
                             0, automatic=True)
                    Repo.touch(state)
                    return None, None
                before = _snapshot(dispatch, state, fronts, source)
                branches = eligible_branches(stage_definition(state.plan, state.stage_key), state.progress)
                try:
                    if len(branches) != 1:
                        raise ValueError('当前阶段没有唯一可推进分支，请核查进度')
                    after = _proposal(dispatch, state, fronts, 'transition', {'branch': branches[0]})
                except ValueError as exc:
                    # Rotate blocked roots behind other ready work, preserving all
                    # gameplay state and evidence for a later retry or manual review.
                    Repo.touch(state)
                    return None, str(exc)
                receipt = _apply(dispatch, state, fronts, source, before, after,
                    operation_key=f'auto:{dispatch.id}:{state.stage_instance}', kind='auto_transition',
                    actor_id=0, reason=f'已发布节点证据满足 {branches[0]} 分支，自动推进阶段')
                return _receipt(receipt, replayed=False), None
        except (ValueError, DatabaseError, TransactionError) as exc:
            return None, _error(exc)

    @staticmethod
    def advance_ready_stages():
        """Scheduler recovery also handles a restart after the last report commits."""
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin
        counts = {'advanced': 0, 'blocked': 0, 'errors': []}
        if not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True):
            return counts
        key = 'ADVENTURER_S1_CAMPAIGN_STAGE_AUTO_BATCH_SIZE'
        limit = max(1, int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])))
        try:
            with db_manager.transaction():
                roots = Repo.ready_roots(limit)
            # Each campaign owns its own short transaction and releases its locks
            # before the next campaign. No provider, HTTP or preview-cache call.
            for root_id in roots:
                receipt, error = S1CampaignStageService.advance_automatic(root_id)
                if error:
                    counts['blocked'] += 1
                    counts['errors'].append({'root_id': root_id, 'message': error})
                    current_app.logger.warning('S1 战役自动推进暂缓 root_id=%s: %s', root_id, error)
                elif receipt:
                    counts['advanced'] += 1
            from plugins.adventurer_s1.services.s1_campaign_continuation_service import S1CampaignContinuationService
            S1CampaignContinuationService.process_pending()
            return counts
        finally:
            db_manager.remove()


def _error(exc):
    original = getattr(exc, 'original_error', exc)
    if isinstance(original, ValueError):
        return str(original)
    current_app.logger.exception('S1 阶段操作失败')
    return '战役操作失败，请重新读取进度后重试'
