"""Host policy around the upstream projection owners, without changing pack sources.

New inputs use full descriptions by default. Validated frozen receipts remain the
authority for an existing expedition, including its reviewer and retry inputs.
"""
from copy import deepcopy
from functools import wraps
import sys

from services.tavern_ai_input_settings import enabled, FLAG

FROZEN = '_portable_frozen_description'
POLICY = '_portable_input_compression_enabled'


def _member_policy(members):
    for member in members:
        if hasattr(member, POLICY):
            return getattr(member, POLICY)
    return enabled()


def _assets(members):
    for member in members:
        for field, kind in (('traits', 'trait'), ('skill_items', 'skill'), ('equipment_items', 'item')):
            for asset in getattr(member, field, None) or []:
                yield asset, kind


def _full(asset, kind, fallback=''):
    return str((getattr(asset, 'custom_desc', None) if kind in {'item', 'companion'} else None)
        or getattr(asset, 'description', None) or fallback or '').strip()


def _replace(module, name, wrapper):
    original = getattr(module, name)
    replacement = wraps(original)(wrapper(original))
    # The pack imports some owners by value. Rebind only exact function aliases
    # inside this pack, never another pack or the control plane.
    for key, loaded in list(sys.modules.items()):
        if key.startswith('plugins.adventurer_s1.') and loaded is not None:
            for alias, value in list(vars(loaded).items()):
                if value is original:
                    setattr(loaded, alias, replacement)
    setattr(module, name, replacement)


def install_routes(app):
    from flask import jsonify
    from routes.auth import login_required
    from utils.decorators import admin_required
    endpoint = 'adventurer_s1.admin_trait_compress'
    original = app.view_functions[endpoint]

    @wraps(original)
    @login_required
    @admin_required
    def compress():
        if not enabled():
            return jsonify(code=400, message='当前酒馆已关闭压缩，请先在 S1 AI 模型配置中开启', data=None), 400
        return original()

    app.view_functions[endpoint] = compress


def install(app):
    app.config[FLAG] = False
    app.config['ADVENTURER_S1_TRAIT_CONTEXT_SELECTION_MODE'] = 'off'
    from plugins.adventurer_s1.services import s1_description_ai_service as descriptions
    from plugins.adventurer_s1.services import s1_trait_description_ai_service as traits
    from plugins.adventurer_s1.services import s1_trait_context_projection_service as projection
    from plugins.adventurer_s1.services import adventurer_s1_prompt_service as prompts
    if getattr(descriptions, '_portable_input_mode_installed', False):
        return

    def asset_text(original):
        def call(asset, *, kind, fallback_description=''):
            if enabled() or getattr(asset, FROZEN, False):
                return original(asset, kind=kind, fallback_description=fallback_description)
            return _full(asset, kind, fallback_description)
        return call

    def trait_text(original):
        def call(trait):
            if enabled() or getattr(trait, FROZEN, False):
                return original(trait)
            return _full(trait, 'trait') or '未知效果的特质'
        return call

    def restore(original):
        def call(members, receipt):
            result = deepcopy(original(members, receipt))
            policy = receipt.get('compression_enabled', True) if isinstance(receipt, dict) else enabled()
            for member in result:
                setattr(member, POLICY, policy)
            for asset, _ in _assets(result):
                setattr(asset, FROZEN, True)
            return result
        return call

    def refresh(original):
        def call(members):
            if enabled():
                return original(members)
            result = deepcopy(list(members or []))
            for member in result:
                setattr(member, POLICY, False)
            for asset, kind in _assets(result):
                field = 'custom_desc_ai' if kind == 'item' else 'description_ai'
                setattr(asset, field, None)
                setattr(asset, field + '_source_hash', None)
                setattr(asset, FROZEN, False)
                if kind == 'trait':
                    asset.trait_context_rule = None
            return result
        return call

    def freeze(original):
        def call(members, **kwargs):
            result = original(members, **kwargs)
            result['compression_enabled'] = _member_policy(members)
            return result
        return call

    def member_block(original):
        def call(member, *args, **kwargs):
            if not _member_policy([member]):
                kwargs['resource_detail_names'] = None
                kwargs['compact_unfocused_resources'] = False
            return original(member, *args, **kwargs)
        return call

    def select(original):
        def call(*args, **kwargs):
            receipt = kwargs.get('frozen_receipt')
            if (kwargs.get('mode_override') is None and isinstance(receipt, dict)
                    and receipt.get('mode') in {'off', 'active', 'shadow'}):
                # The original owner still validates identities and hashes.
                reused = original(*args, **{**kwargs, 'mode_override': receipt['mode']})
                if reused.reused_frozen_receipt:
                    return reused
            if not enabled():
                kwargs['mode_override'] = 'off'
            return original(*args, **kwargs)
        return call

    def process(original):
        def call(payload, **kwargs):
            if enabled():
                return original(payload, **kwargs)
            if (payload or {}).get('generation_mode') == 'description_feedback':
                from plugins.adventurer_s1.services.s1_description_feedback_service import _finish
                _finish(payload, 'retryable')
                return 0, 0
            items = (payload or {}).get('items') or []
            if (payload or {}).get('generation_mode') == 'trait_context_review_only':
                descriptions._release_unqueued_trait_context_review_items(items)
            else:
                descriptions._release_unqueued_items(items)
            return 0, 0
        return call

    _replace(descriptions, 'frozen_asset_description_for_ai', asset_text)
    _replace(traits, 'trait_text_for_expedition_ai', trait_text)
    _replace(descriptions, 'restore_frozen_member_description_projection', restore)
    _replace(descriptions, 'build_frozen_member_description_projection_receipt', freeze)
    _replace(descriptions, 'refresh_frozen_member_description_ai_context', refresh)
    _replace(prompts, 'build_s1_member_block', member_block)
    _replace(projection, 'build_s1_trait_context_projection', select)
    _replace(descriptions, 'process_description_compression_task', process)
    _replace(traits, 'compress_traits', lambda original: lambda *args, **kwargs:
        original(*args, **kwargs) if enabled() else (0, 0))
    descriptions._portable_input_mode_installed = True
