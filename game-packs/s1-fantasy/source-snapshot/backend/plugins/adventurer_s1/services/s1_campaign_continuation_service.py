"""Prepare once, review outside SQL, then atomically join the existing stage chain."""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime, timedelta
import threading
from uuid import uuid4
from flask import current_app

from db_repository import db_manager
from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository as Stages
from db_repository.s1_campaign_continuation_repository import S1CampaignContinuationRepository as Repo
from plugins.adventurer_s1.services.s1_campaign_continuation_contract import (
    digest, limit, text, normalize_proposal, normalize_generated_proposal, normalize_review, updated_plan,
    input_messages,
)
from plugins.adventurer_s1.services.s1_campaign_stage_contract import eligible_branches, stage_definition
from plugins.adventurer_s1.services.s1_campaign_stage_service import (
    _identity, _snapshot, _evidence, _proposal, _apply, _receipt, _error,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService as Snapshots
from plugins.adventurer_s1.services.s1_ai_player_name_service import frozen_player_names
from plugins.adventurer_s1.services.s1_campaign_time_contract import project_action_window_context
from db_repository.s1_campaign_local_world_repository import (
    load_campaign_local_world, campaign_location_scopes, published_campaign_loci,
    published_campaign_changed_locations,
)
from plugins.adventurer_s1.services.s1_expedition_locus_service import (
    LOCATION_SCOPE_KEY_PREFIX, extract_location_scope_keys, location_scope_key,
    normalize_expedition_locus_snapshot,
)
from utils.time_utils import to_utc_iso

OPERATION = 'campaign_continuation_v1'


def _location_refs(dispatch):
    maximum = limit('MAX_LOCAL_LOCATIONS')
    if maximum < 1:
        raise ValueError('战役当地事实读取上限必须为正数')
    references = set()
    def add(dungeon, identifier):
        if type(identifier) is not int or identifier < 1:
            raise ValueError('战役地点引用身份无效')
        references.add((dungeon, identifier))
        if len(references) > maximum:
            raise ValueError('战役关联地点超过完整快照上限')
    for dungeon, scopes in campaign_location_scopes(dispatch.campaign_root_id, batch_size=maximum):
        for key in extract_location_scope_keys(scopes):
            identifier = key[len(LOCATION_SCOPE_KEY_PREFIX):]
            if (not identifier.isascii() or not identifier.isdigit()
                    or int(identifier) < 1 or location_scope_key(int(identifier)) != key):
                raise ValueError('战役地点作用域不是准确的持久地点引用')
            add(dungeon, int(identifier))
    for log_id, dungeon, raw in published_campaign_loci(dispatch.id, batch_size=maximum):
        if not raw:
            continue  # Older published expeditions may have no frozen locus.
        locus = normalize_expedition_locus_snapshot(
            raw, expected_target_dungeon=dungeon, expected_expedition_log_id=log_id)
        if not locus:
            raise ValueError('已发布战役日志的地点身份无效')
        if locus['kind'] == 'known_location':
            add(dungeon, locus['location_id'])
    for identifier, dungeon in published_campaign_changed_locations(dispatch.id, maximum=maximum):
        add(dungeon, identifier)
    return sorted(references)


def _freeze(dispatch, state, fronts, *, lock_world=False):
    from plugins.adventurer_s1.services.s1_campaign_stage_contract import _limit as stage_limit
    if not 0 < limit('HEARTBEAT_SECONDS') < limit('LEASE_SECONDS'):
        raise ValueError('阶段衔接心跳必须短于租约')
    if not dispatch or not state or not dispatch.discovered_at or dispatch.ended_at or state.status != 'ready':
        raise ValueError('只有已发现且当前阶段条件满足的战役才能准备衔接')
    if dispatch.active_log_id or Stages.busy([f.id for f in fronts]):
        raise ValueError('仍有小队执行或世界事实待处理，请等待战报发布')
    source = _identity()
    stage = stage_definition(state.plan, state.stage_key)
    branches = eligible_branches(stage, state.progress)
    if not branches:
        raise ValueError('当前阶段条件尚未满足')
    ids = {v['evidence_log_id'] for v in state.progress.values() if v['result'] != 'pending'}
    ids.update(Repo.latest_evidence_ids(dispatch.id))
    if len(ids) > limit('MAX_EVIDENCE'):
        raise ValueError('阶段证据数量超过衔接上限')
    rows = Stages.evidences(dispatch.id, ids)
    for key, entry in state.progress.items():
        if entry['result'] != 'pending':
            _evidence(dispatch, state, key, entry['evidence_log_id'], rows, terminal=True)
    evidence, names = [], set()
    for log_id in sorted(rows):
        report, log = rows[log_id]
        full = log.full_log or {}
        events = full.get('events')
        if not isinstance(events, list) or not events or any(not isinstance(e, str) or not e.strip() for e in events):
            raise ValueError('战报缺少完整已发布正文，无法核验衔接')
        names.update(frozen_player_names(full))
        evidence.append({'log_id': log.id, 'dungeon': log.target_dungeon,
                         'created_at': to_utc_iso(log.created_at),
                         'published_at': to_utc_iso(report.published_at),
                         'report_revision': report.revision, 'report': {
                             k: deepcopy(report.report[k]) for k in (
                                 'public_title', 'public_summary', 'outcome', 'node_result', 'node_evidence')
                             if k in report.report},
                         'events': deepcopy(events)})
        frozen_dispatch = full.get('campaign_dispatch')
        if isinstance(frozen_dispatch, dict) and 'action_window_context' in frozen_dispatch:
            evidence[-1]['action_window_context'] = project_action_window_context(
                frozen_dispatch['action_window_context'])
    fields = ('id', 'revision', 'title', 'summary', 'status', 'base_dungeon', 'campaign_role',
              'campaign_agenda', 'power_profile', 'power_tier')
    result = {'root_id': dispatch.campaign_root_id, 'before': _snapshot(dispatch, state, fronts, source),
              'plan': deepcopy(state.plan), 'eligible_branches': branches,
              'entered_stage_keys': sorted(Stages.entered_stage_keys(
                  dispatch.id, state.stage_key, state.plan['stages'][0]['key'])),
              'fronts': [{k: deepcopy(getattr(f, k)) for k in fields} for f in sorted(fronts, key=lambda f: f.id)],
              'local_world': load_campaign_local_world(
                  [f.base_dungeon for f in fronts if f.campaign_role == 'dungeon_front'],
                  location_refs=_location_refs(dispatch),
                  max_locations=limit('MAX_LOCAL_LOCATIONS'),
                  actor_batch_size=limit('LOCAL_ACTOR_BATCH_SIZE'),
                  lock=lock_world),
              'actors': Repo.related_actors([f.id for f in fronts], lock=lock_world), 'evidence': evidence,
              'factions': Repo.related_factions([f.id for f in fronts], lock=lock_world),
              # Stable event times, never a ticking now() in the proposal identity.
              'timeline': {'campaign_started_at': to_utc_iso(dispatch.created_at),
                           'campaign_discovered_at': to_utc_iso(dispatch.discovered_at)},
              'player_names': sorted(names),
              'runtime_source_commit': current_app.config.get('S1_RUNTIME_CONTRACT', {}).get('source_commit'),
              'contract_limits': {'max_changed_stages': limit('MAX_STAGES'),
                  'max_total_stages': stage_limit('MAX_STAGES'), 'max_nodes_per_stage': stage_limit('MAX_NODES'),
                  'max_successors': len([f for f in fronts if f.campaign_role == 'dungeon_front']),
                  'max_evidence_ids': limit('MAX_EVIDENCE'), 'max_text_chars': limit('TEXT_LIMIT'),
                  'max_title_chars': 120, 'max_key_chars': 64},
              'contract_version': 7}
    result['input_hash'] = digest(result)
    input_messages(result)
    return result


def _view(job):
    return {'input_hash': job.input_hash, 'status': job.status, 'attempts': job.attempts,
            'error': job.error, 'proposal': deepcopy(job.proposal), 'review': deepcopy(job.review),
            'automatic': bool(job.request.get('automatic')), 'admin_user_id': job.admin_user_id,
            'reason': job.request['reason']}


def _enqueue(dispatch, state, fronts, body, actor_id, *, automatic=False):
    snapshot = _freeze(dispatch, state, fronts)
    if not isinstance(body, dict):
        raise ValueError('衔接请求必须为对象')
    if 'retry' in body and type(body['retry']) is not bool:
        raise ValueError('retry 必须为布尔值')
    request = {'reason': text(body.get('reason'), '衔接理由'), 'automatic': automatic}
    if body.get('proposal') is not None:
        if not isinstance(body['proposal'], dict):
            raise ValueError('衔接草案必须为对象')
        request['proposal'] = normalize_proposal(
            {**body['proposal'], 'input_hash': snapshot['input_hash']}, snapshot)
    old = Repo.get(dispatch.id, lock=True)
    if old and old.status == 'processing' and old.lease_until and old.lease_until > _portable_utc_now():
        return _view(old)
    if old and old.status != 'stale' and old.input_hash == snapshot['input_hash']:
        # An automatic tick cannot replace a human's reviewed preview or restart
        # rejected/failed work indefinitely. Retries remain bounded per input.
        if automatic or (old.request == request and not body.get('retry')):
            return _view(old)
    return _view(Repo.save(dispatch.id, snapshot, request, actor_id))


def _touch(dispatch_id, token):
    with db_manager.transaction():
        job = Repo.get(dispatch_id, lock=True)
        if not job or job.status != 'processing' or job.owner_token != token:
            return False
        if not job.lease_until or job.lease_until <= _portable_utc_now():
            return False
        job.lease_until = _portable_utc_now() + timedelta(seconds=limit('LEASE_SECONDS'))
        return True


def _heartbeat(app, dispatch_id, token, stop):
    with app.app_context():
        while not stop.wait(limit('HEARTBEAT_SECONDS')):
            try:
                if not _touch(dispatch_id, token):
                    return
            except Exception:
                current_app.logger.exception('S1 阶段衔接租约续期失败 dispatch_id=%s', dispatch_id)
            finally:
                db_manager.remove()


def _owned(job, token):
    return bool(job and job.status == 'processing' and job.owner_token == token
                and job.lease_until and job.lease_until > _portable_utc_now())


class S1CampaignContinuationService:
    @staticmethod
    def status(root_id):
        try:
            with db_manager.transaction():
                _identity()
                return Repo.status_row(root_id), None
        except Exception as exc:
            return None, _error(exc)
        finally:
            db_manager.remove()

    @staticmethod
    def prepare(root_id, body, admin_user_id):
        try:
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                return _enqueue(dispatch, state, fronts, body, admin_user_id), None
        except Exception as exc:
            return None, _error(exc)
        finally:
            db_manager.remove()

    @staticmethod
    def process(root_id):
        token, dispatch_id = uuid4().hex, None
        try:
            with db_manager.transaction():
                prepared = Repo.for_root(root_id)
                replay = ((prepared.input_hash, digest(prepared.proposal))
                          if prepared and prepared.status == 'prepared' and prepared.request['automatic'] else None)
            db_manager.remove()
            if replay:
                with db_manager.transaction():
                    dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                    job = Repo.get(dispatch.id, lock=True) if dispatch else None
                    if not job or job.input_hash != replay[0] or job.status != 'prepared':
                        return
                    if dispatch.active_log_id or Stages.busy([f.id for f in fronts]):
                        job.updated_at = _portable_utc_now()
                        return
                    try:
                        fresh = _freeze(dispatch, state, fronts)['input_hash'] == replay[0]
                    except ValueError:
                        fresh = False
                    if not fresh:
                        job.status, job.error = 'stale', '待执行衔接的阶段或证据已变化'
                        return
                db_manager.remove()
                S1CampaignContinuationService._execute(root_id, *replay, 0)
                return
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                if not dispatch:
                    return
                dispatch_id = dispatch.id
                job = Repo.get(dispatch_id, lock=True)
                now = _portable_utc_now()
                if not job or job.status not in ('pending', 'processing', 'failed'):
                    return
                if job.status == 'processing' and job.lease_until and job.lease_until > now:
                    return
                if job.attempts >= limit('MAX_ATTEMPTS'):
                    job.status, job.error = 'failed', '衔接重试次数已用尽，请核查原因后重新准备'
                    return
                if job.status == 'failed' and (now - job.updated_at).total_seconds() < limit('RETRY_SECONDS'):
                    return
                if dispatch.active_log_id or Stages.busy([f.id for f in fronts]):
                    job.updated_at = now
                    return
                try:
                    snapshot = _freeze(dispatch, state, fronts)
                except ValueError as exc:
                    job.status, job.error = 'stale', str(exc)
                    return
                if snapshot['input_hash'] != job.input_hash:
                    job.status, job.error = 'stale', '战役或证据已变化，请重新准备'
                    return
                job.status, job.owner_token = 'processing', token
                job.lease_until = now + timedelta(seconds=limit('LEASE_SECONDS'))
                job.attempts += 1
                job.updated_at = now
                snapshot, request, attempt = deepcopy(job.snapshot), deepcopy(job.request), job.attempts
                previous_proposal = deepcopy(job.proposal)
            db_manager.remove()
            stop = threading.Event()
            heartbeat = threading.Thread(target=_heartbeat,
                args=(current_app._get_current_object(), dispatch_id, token, stop), daemon=True)
            heartbeat.start()
            try:
                from plugins.adventurer_s1.services import s1_campaign_continuation_ai_service as AI
                proposal = previous_proposal or request.get('proposal') or AI.call(snapshot, attempt=attempt)
                proposal = (normalize_proposal if request.get('proposal') else normalize_generated_proposal)(proposal, snapshot)
                with db_manager.transaction():
                    job = Repo.get(dispatch_id, lock=True)
                    if not _owned(job, token):
                        return
                    job.proposal = proposal
                db_manager.remove()
                review = normalize_review(AI.call(snapshot, proposal, attempt=attempt), snapshot, proposal)
                with db_manager.transaction():
                    dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                    job = Repo.get(dispatch_id, lock=True)
                    if not _owned(job, token):
                        return
                    if _freeze(dispatch, state, fronts)['input_hash'] != snapshot['input_hash']:
                        job.status, job.error = 'stale', '核验期间战役或世界事实已变化'
                        return
                    job.review = review
                    job.status = 'prepared' if review['decision'] == 'allow' else 'rejected'
                    job.error = None if review['decision'] == 'allow' else review['reason']
                    job.owner_token = job.lease_until = None
                    job.updated_at = _portable_utc_now()
                db_manager.remove()
                if request['automatic'] and review['decision'] == 'allow':
                    _, error = S1CampaignContinuationService._execute(root_id, snapshot['input_hash'], digest(proposal), 0)
                    if error:
                        current_app.logger.warning('S1 自动衔接待重试 root_id=%s: %s', root_id, error)
            finally:
                stop.set()
                heartbeat.join(timeout=limit('HEARTBEAT_JOIN_SECONDS'))
        except Exception as exc:
            current_app.logger.exception('S1 阶段衔接准备失败 root_id=%s', root_id)
            if dispatch_id is not None:
                with db_manager.transaction():
                    job = Repo.get(dispatch_id, lock=True)
                    if _owned(job, token):
                        job.status, job.error = 'failed', str(exc)[:limit('TEXT_LIMIT')]
                        job.owner_token = job.lease_until = None
                        job.updated_at = _portable_utc_now()
        finally:
            db_manager.remove()

    @staticmethod
    def process_pending():
        with db_manager.transaction():
            roots = Repo.pending_roots(limit('BATCH_SIZE'), limit('MAX_ATTEMPTS'), limit('RETRY_SECONDS'))
        db_manager.remove()
        for root_id in roots:
            S1CampaignContinuationService.process(root_id)

    @staticmethod
    def preview(root_id, admin_user_id):
        try:
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                snapshot = _freeze(dispatch, state, fronts)
                job = Repo.get(dispatch.id, lock=True)
                if not job or job.status != 'prepared' or job.input_hash != snapshot['input_hash']:
                    raise ValueError('衔接尚未通过审核或已过期，请先准备并读取结果')
                proposal = normalize_proposal(job.proposal, snapshot)
                review = normalize_review(job.review, snapshot, proposal)
                if review['decision'] != 'allow':
                    raise ValueError('衔接审核未通过')
                payload = {'root_id': root_id, 'source': snapshot['before']['source'],
                           'input_hash': job.input_hash, 'proposal_hash': digest(proposal)}
                result = {'before': snapshot['before'], 'proposal': proposal, 'review': review,
                          'evidence': snapshot['evidence'], 'local_world': snapshot['local_world'],
                          'timeline': snapshot['timeline']}
            db_manager.remove()
            result['preview_token'] = Snapshots.store(OPERATION, admin_user_id, payload, allow_signed_fallback=True)
            if not result['preview_token']:
                raise ValueError('无法保存衔接预览')
            return result, None
        except Exception as exc:
            return None, _error(exc)

    @staticmethod
    def execute(root_id, preview_token, admin_user_id):
        db_manager.remove()
        payload = Snapshots.load(OPERATION, admin_user_id, preview_token, allow_signed_fallback=True)
        if not payload or payload.get('root_id') != root_id:
            return None, '衔接预览无效或不属于本次操作'
        with db_manager.transaction():
            if payload['source'] != _identity():
                return None, '区服或部署身份已变化，请重新预览'
        return S1CampaignContinuationService._execute(
            root_id, payload['input_hash'], payload['proposal_hash'], admin_user_id)

    @staticmethod
    def _execute(root_id, input_hash, proposal_hash, admin_user_id):
        try:
            with db_manager.transaction():
                dispatch, state, fronts = Stages.bundle(root_id, lock=True)
                if not dispatch:
                    raise ValueError('战役不存在')
                key = f'continuation:{dispatch.id}:' + input_hash[:32]
                old_receipt = Stages.receipt(key)
                if old_receipt:
                    if old_receipt.admin_user_id != admin_user_id:
                        raise ValueError('衔接回执执行者不匹配')
                    return _receipt(old_receipt, replayed=True), None
                snapshot = _freeze(dispatch, state, fronts, lock_world=True)
                job = Repo.get(dispatch.id, lock=True)
                if not job or job.status != 'prepared' or job.input_hash != input_hash or snapshot['input_hash'] != input_hash:
                    raise ValueError('衔接或世界事实已变化，请重新准备')
                proposal = normalize_proposal(job.proposal, snapshot)
                if digest(proposal) != proposal_hash:
                    raise ValueError('预览提案已变化')
                review = normalize_review(job.review, snapshot, proposal)
                if review['decision'] != 'allow':
                    raise ValueError('衔接审核未通过')
                if admin_user_id == 0 and (not job.request['automatic'] or state.plan.get('advance_mode') != 'auto'):
                    raise ValueError('此衔接需要管理员确认')
                created = Repo.create_successors(fronts, proposal['successors'])
                before = snapshot['before']
                state.plan = updated_plan(proposal, snapshot)
                heads = [f for f in fronts if f.is_chain_head is not False] + created
                after = _proposal(dispatch, state, heads, 'transition', {'branch': proposal['branch']})
                dispatch.public_summary = proposal['public_summary']
                receipt = _apply(dispatch, state, heads, snapshot['before']['source'], before, after,
                    operation_key=key, kind='continuation', actor_id=admin_user_id, reason=job.request['reason'])
                receipt.before = {**receipt.before, 'plan': snapshot['plan'],
                    'local_world': snapshot['local_world'], 'timeline': snapshot['timeline']}
                receipt.after = {**receipt.after, 'plan': deepcopy(state.plan),
                    'continuation': proposal, 'review': review,
                    'successor_ids': [f.id for f in created], 'evidence_log_ids': proposal['evidence_log_ids']}
                job.status, job.updated_at = 'applied', _portable_utc_now()
                return _receipt(receipt, replayed=False), None
        except Exception as exc:
            return None, _error(exc)
        finally:
            db_manager.remove()
