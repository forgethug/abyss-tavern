"""Non-blocking, content-bound reports from a draft to the description owners.

Reports cannot change a live encounter or authorize asset mutations. Cache values
remain readable during review; publication uses a lease and a cache-version CAS.
"""
from runtime_core.time import utc_now as _portable_utc_now
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import logging
from uuid import uuid4
from sqlalchemy.exc import IntegrityError

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache as Cache

logger = logging.getLogger(__name__)
MAX_REPORTS = 3
LEASE_SECONDS = 3600

FEEDBACK_PROMPT = """【资产描述疑点反馈】
推演中自然发现资产描述需要重新审核时，可附加 asset_description_feedback 字符串数组，只列对应 review_ref，至多 3 项；没有疑点时省略。无需另行遍历全队资产，也无需解释原因或提出修复方案。
后台会复查当前机制、无关旧经历与既有输入资格。反馈本身不改变资产效力；当前交锋仍按已有能力作用边界推演。
"""


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def register_feedback_source(catalog, row, asset, *, member_id, kind, fallback=''):
    """Called next to the exact visible row, before member-name anonymization."""
    if catalog is None:
        return
    asset_id = getattr(asset, 'id', None)
    if type(asset_id) is not int or asset_id <= 0 or type(member_id) is not int:
        return
    from plugins.adventurer_s1.services.s1_description_ai_service import description_source_hash
    source = str((getattr(asset, 'custom_desc', None) if kind in {'item', 'companion'} else None)
                 or getattr(asset, 'description', None) or fallback or '').strip()
    visible = row.get('description')
    if not source or not isinstance(visible, str) or not visible.strip():
        return
    # Request-local references keep database identities and cache versions private.
    ref = f'D{len(catalog) + 1}'
    catalog[ref] = {'source_hash': description_source_hash(source, kind=kind),
                    'kind': kind, 'name': str(getattr(asset, 'name', '') or ''),
                    'description': source, 'observed_description': visible,
                    'member_id': member_id, 'asset_id': asset_id}
    if kind == 'companion':
        from plugins.adventurer_s1.services.s1_ai_input_policy_service import companion_policy_description
        policy_source = companion_policy_description(source, getattr(asset, 'companion_lineage', []))
        if policy_source:
            catalog[ref]['policy_source'] = policy_source
    row['review_ref'] = ref


def normalize_feedback(value):
    """Optional telemetry has no authority to reject an otherwise valid draft."""
    if not isinstance(value, list) or len(value) > MAX_REPORTS:
        return []
    return list(dict.fromkeys(ref for ref in value
                              if isinstance(ref, str) and 0 < len(ref) <= 120))


def _cache_version(row):
    return _digest({key: getattr(row, key) for key in (
        'description_ai', 'status', 'audit_status', 'trait_context_rule',
        'trait_context_audit_receipt', 'ai_input_policy')})


def enqueue_description_feedback(reports, catalog, *, expedition_log_id=None, member_id_to_name=None):
    """Reserve reviewed caches too, without invalidating their active values."""
    from plugins.adventurer_s1.services import s1_description_ai_service as D
    from services.async_task_service import AsyncTaskService
    if not D.description_ai_summaries_enabled():
        return 0
    db_manager.remove()
    count = 0
    requested = []
    for ref in normalize_feedback(reports):
        item = deepcopy(catalog.get(ref))
        if not item:
            continue
        requested.append(item)
        if item.get('policy_source'):
            # The backend supplies the complete frozen lineage to the existing
            # companion-policy owner; the draft still reports just one reference.
            source = item['policy_source']
            requested.append({'source_hash': D.description_source_hash(source, kind='companion_policy'),
                              'kind': 'companion_policy', 'name': item['name'],
                              'description': source, 'observed_description': source,
                              'member_id': item['member_id'], 'asset_id': item['asset_id']})
    for item in requested:
        key = item['source_hash']
        request_id = _digest([key, item['observed_description']])
        token, now = uuid4().hex, _portable_utc_now()
        with db_manager.transaction():
            row = Cache.query.filter_by(source_hash=key).populate_existing().with_for_update().first()
            if row is None:
                if item['observed_description'] != item['description']:
                    continue
                try:
                    with db.session.begin_nested():
                        row = Cache(source_hash=key, description_kind=item['kind'],
                                    description_ai='', status='keep_original', audit_status='not_sampled')
                        db.session.add(row)
                        db.session.flush()
                except IntegrityError:
                    continue
            if row.status in {'pending', 'processing', 'retryable'}:
                continue
            current = row.description_ai if row.status == 'active' and row.audit_status == 'passed' else item['description']
            if current != item['observed_description']:
                continue
            receipt = row.description_feedback_receipt or {}
            if request_id in receipt.get('completed_requests', []):
                continue
            if receipt.get('retry_after') and receipt['retry_after'] > now.isoformat():
                continue
            if receipt.get('status') in {'pending', 'processing'} and receipt.get('lease_until', '') > now.isoformat():
                continue
            baseline = _cache_version(row)
            row.description_feedback_receipt = {
                'request_id': request_id, 'token': token, 'reservation_token': token, 'status': 'pending',
                'lease_until': (now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                'completed_requests': receipt.get('completed_requests', [])[-8:],
                'expedition_log_id': expedition_log_id,
            }
        payload = {'batch_id': token, 'generation_mode': 'description_feedback',
                   'expedition_log_id': expedition_log_id, 'item': item,
                   'baseline': baseline, 'token': token,
                   'request_id': request_id, 'member_id_to_name': dict(member_id_to_name or {})}
        db_manager.remove()
        try:
            queued = AsyncTaskService.submit_task('ai_s1_description_compress', payload)
            if not queued:
                queued = AsyncTaskService.is_task_enqueued_or_running(
                    'ai_s1_description_compress', {'batch_id': token})
        except Exception:
            queued = False
            logger.exception('S1 description feedback queue failed source=%s', key)
        if queued:
            count += 1
        else:
            _finish(payload, 'retryable')
    logger.info('S1 description feedback queued log=%s count=%s', expedition_log_id, count)
    return count


def _finish(payload, status, *, summary=None, policy=None, model=None):
    with db_manager.transaction():
        row = Cache.query.filter_by(source_hash=payload['item']['source_hash']).populate_existing().with_for_update().first()
        if row is None:
            return False
        receipt = row.description_feedback_receipt or {}
        if receipt.get('token') != payload['token']:
            return False
        if _cache_version(row) != payload['baseline']:
            status, summary, policy = 'superseded', None, None
        if summary is not None:
            row.description_ai = summary
            row.status, row.audit_status = 'active', 'passed'
            row.audited_at, row.model_used = _portable_utc_now(), model
        if policy is not None:
            row.ai_input_policy = policy
        updated = {**receipt, 'status': status, 'finished_at': _portable_utc_now().isoformat()}
        if status in {'completed', 'superseded'}:
            updated['completed_requests'] = (receipt.get('completed_requests', []) + [payload['request_id']])[-8:]
        else:
            updated['retry_after'] = (_portable_utc_now() + timedelta(hours=1)).isoformat()
        updated['summary_updated'] = summary is not None
        updated['policy_updated'] = policy is not None
        row.description_feedback_receipt = updated
        return status == 'completed'


def process_description_feedback(payload, *, restart_recovered=False):
    from plugins.adventurer_s1.services import s1_description_ai_service as D
    from plugins.adventurer_s1.services.s1_ai_input_policy_service import normalize_ai_input_policy
    item, token = payload['item'], payload['token']
    key = item['source_hash']
    if D.description_source_hash(item['description'], kind=item['kind']) != key:
        return 0, 0
    with db_manager.transaction():
        row = Cache.query.filter_by(source_hash=key).populate_existing().with_for_update().first()
        receipt = row.description_feedback_receipt if row else None
        if not receipt or receipt.get('reservation_token') != token or receipt.get('status') not in (
                {'pending', 'processing'} if restart_recovered else {'pending'}):
            return 0, 0
        if _cache_version(row) != payload['baseline']:
            row.description_feedback_receipt = {**receipt, 'status': 'superseded'}
            return 0, 0
        # Rotate ownership even for restart recovery: an old worker cannot publish.
        token = uuid4().hex
        payload = {**payload, 'token': token}
        row.description_feedback_receipt = {**receipt, 'token': token, 'status': 'processing'}
        context = deepcopy(row.trait_context_rule)
    db_manager.remove()
    try:
        model, summary, policy = None, None, None

        def audit(candidate):
            audit_item = {'review_key': key, 'kind': item['kind'], 'name': item['name'],
                          'description': item['description'], 'description_ai': candidate.get('description_ai'),
                          'summary_decision': candidate['decision'],
                          'trait_context_rule': candidate.get('trait_context_rule', context),
                          'target_chars': D.get_description_ai_max_chars(),
                          'ai_input_policy_required': not bool(item.get('policy_source'))}
            db_manager.remove()
            results, _ = D.audit_description_summary_samples(
                [audit_item], expedition_log_id=payload.get('expedition_log_id'))
            result = (results or {}).get(key) or {}
            return result, normalize_ai_input_policy(result.get('ai_input_policy'),
                       source_hash=key, description=item['description'])

        # Recheck the existing summary first. A false alarm costs no regeneration.
        has_summary = item['observed_description'] != item['description']
        policy_only = item['kind'] == 'companion_policy'
        result = {}
        if has_summary or policy_only:
            result, policy = audit({'decision': 'compressed' if has_summary else 'keep_original',
                                    'description_ai': item['observed_description'] if has_summary else None})
            if not result:
                _finish(payload, 'retryable')
                return 1, 0
        verdict = (result.get('summary_review') or {}).get('decision')
        if not policy_only and (not has_summary or verdict == 'rejected'):
            db_manager.remove()
            proposals, model = D._call_description_compress_ai(
                [item], expedition_log_id=payload.get('expedition_log_id'))
            candidate = (proposals or {}).get(key)
            if not candidate:
                _finish(payload, 'retryable', policy=policy)
                return 1, 0
            result, new_policy = audit(candidate)
            policy = new_policy or policy
            verdict = (result.get('summary_review') or {}).get('decision')
            proposed = candidate.get('description_ai')
            if (candidate['decision'] == 'compressed' and verdict == 'approved'
                    and D._valid_compressed_summary(item['description'], proposed)):
                summary = proposed
        complete = verdict in {'approved', 'rejected', 'not_applicable'} and (
            policy is not None or bool(item.get('policy_source')))
        stored = _finish(payload, 'completed' if complete else 'retryable',
                         summary=summary, policy=policy, model=model)
        return 1, int(stored)
    except Exception:
        db_manager.rollback()
        _finish(payload, 'retryable')
        logger.exception('S1 description feedback review failed source=%s', key)
        return 1, 0
