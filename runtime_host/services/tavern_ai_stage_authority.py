"""Server-issued permissions for an explicitly disabled independent AI stage.

These records describe administrator policy, never an AI judgment. Signatures
bind the exact candidate and runtime, including across checkpoint recovery.
"""
import copy
import hashlib
import hmac
import json

from flask import current_app
from services.tavern_ai_stage_settings import enabled, read_settings


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _signature(purpose, payload):
    body = [current_app.config['GAME_RUNTIME_ID'], purpose, payload]
    return hmac.new(str(current_app.config['SECRET_KEY']).encode(),
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode(),
        hashlib.sha256).hexdigest()


def issue(stage, purpose, payload):
    if enabled(stage):
        raise ValueError('Independent AI stage is enabled')
    record = {'source': 'administrator_policy', 'stage': stage,
        'status': 'skipped_by_admin', 'revision': read_settings()['revision'],
        'payload': copy.deepcopy(payload)}
    return {**record, 'signature': _signature(purpose, record)}


def verify(record, purpose, payload):
    if not isinstance(record, dict) or record.get('payload') != payload:
        return False
    unsigned = {k: v for k, v in record.items() if k != 'signature'}
    signature = record.get('signature')
    return (record.get('source') == 'administrator_policy'
        and record.get('status') == 'skipped_by_admin' and isinstance(signature, str)
        and hmac.compare_digest(signature, _signature(purpose, unsigned)))


def authorize_fate(context):
    return {'context_id': context['context_id'],
        'items': [{'action_ref': row['action_ref'], 'decision': 'admin_original_proposal',
            'successor_refs': []} for row in context['actions']],
        'admin_policy': issue('expedition_observer', 'fate', context)}


def verified_fate(value, context):
    if not isinstance(value, dict) or not verify(value.get('admin_policy'), 'fate', context):
        return None
    expected = {'context_id': context['context_id'],
        'items': [{'action_ref': row['action_ref'], 'decision': 'admin_original_proposal',
            'successor_refs': []} for row in context['actions']],
        'admin_policy': value['admin_policy']}
    if value.get('context_id') != expected['context_id'] or not isinstance(value.get('items'), list):
        return None
    if len(value['items']) != len(expected['items']):
        return None
    for actual, original in zip(value['items'], expected['items'], strict=True):
        # The final consumer may revoke an action after its frozen asset changed.
        # Revocation never authorizes a different action or a successor.
        revoked = {'action_ref': original['action_ref'], 'decision': 'preserve_existing',
            'successor_refs': []}
        if actual == original:
            continue
        if actual != revoked and actual != {**revoked, 'evidence_event_indexes': []}:
            return None
    expected['items'] = copy.deepcopy(value['items'])
    return copy.deepcopy(expected)
