"""Describe effective region-owned AI routes without exposing credentials."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


_NAMESPACE_RE = re.compile(r'\A[a-z0-9._:-]{1,100}\Z', re.ASCII)
_ALLOWED_PROVIDERS = frozenset({
    'anthropic_compatible',
    'deepseek',
    'mimo',
    'openai_compatible',
    'openrouter_deepseek_v4_pro_baidu',
    'openrouter_deepseek_v4_pro_official',
})


def _canonical_json_bytes(value: object) -> bytes:
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def _fingerprint(secret: str) -> str:
    return 'sha256=' + hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _https_base(value: object) -> str:
    api_base = str(value or '').strip().rstrip('/')
    try:
        parsed = urlsplit(api_base)
        parsed.port
    except ValueError as exc:
        raise RuntimeError('二区 AI provider URL 无效') from exc
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise RuntimeError('二区 AI provider 必须使用 HTTPS')
    return api_base


def _credential(value: object) -> str:
    credential = str(value or '')
    if (
        credential != credential.strip()
        or len(credential.encode('utf-8')) < 16
        or any(ord(character) < 32 for character in credential)
    ):
        raise RuntimeError('二区 AI provider 凭据未配置')
    return credential


def _namespaces(app) -> tuple[str, str]:
    budget = str(app.config.get('S1_RUNTIME_AI_BUDGET_NAMESPACE') or '')
    rate_limit = str(
        app.config.get('S1_RUNTIME_AI_RATE_LIMIT_NAMESPACE') or ''
    )
    if not _NAMESPACE_RE.fullmatch(budget) or not _NAMESPACE_RE.fullmatch(
        rate_limit
    ):
        raise RuntimeError('二区 AI 隔离命名空间无效')
    return budget, rate_limit


def build_degraded_ai_isolation_receipt(app) -> dict:
    """Prove isolation while allowing non-AI gameplay before credentials exist."""
    budget, rate_limit = _namespaces(app)
    feature_routes: list[dict] = []
    return {
        'ready': False,
        'credential_source': 'region2_unconfigured',
        'budget_mode': 'dedicated_provider_account_and_database_ledger',
        'budget_namespace': budget,
        'rate_limit_mode': 'runtime_process',
        'rate_limit_namespace': rate_limit,
        'feature_count': 0,
        'feature_routes': feature_routes,
        'active_providers': [],
        'assignment_digest': (
            'sha256=' + hashlib.sha256(
                _canonical_json_bytes(feature_routes)
            ).hexdigest()
        ),
        'credential_fingerprints': {},
        'mimo_mode': 'unconfigured',
        'mimo_credential_fingerprint': None,
    }


def build_ai_isolation_receipt(app) -> dict:
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
        _AI_FEATURE_KEYS,
    )
    from services import ai_runtime_config_service as shared_ai_runtime_config

    budget, rate_limit = _namespaces(app)
    # Read the Runtime-owned atomic config once. Resolving every feature
    # through the public helper would re-read and parse the same file dozens
    # of times, and a concurrent admin update could mix two revisions into one
    # readiness receipt.
    config_snapshot = dict(app.config)
    shared_ai_runtime_config.sync_runtime_config_from_env(config_snapshot)
    feature_routes = []
    provider_fingerprints: dict[str, set[str]] = {}
    for feature in sorted(_AI_FEATURE_KEYS):
        profile = shared_ai_runtime_config.resolve_ai_profile(
            feature,
            scope='s1',
            config=config_snapshot,
            sync_from_env=False,
        )
        profile_id = str(profile.get('id') or '').strip()
        provider = str(profile.get('provider') or '').strip()
        model = str(profile.get('model') or '').strip()
        if not profile_id or provider not in _ALLOWED_PROVIDERS or not model:
            raise RuntimeError('二区 AI feature assignment 无效')
        primary_api_base = _https_base(profile.get('api_base'))
        primary_credential = _credential(profile.get('api_key'))
        primary_fingerprint = _fingerprint(primary_credential)
        provider_fingerprints.setdefault(provider, set()).add(
            primary_fingerprint
        )
        backup = None
        backup_credential = str(profile.get('backup_api_key') or '').strip()
        if backup_credential and backup_credential != primary_credential:
            backup_credential = _credential(backup_credential)
            backup_fingerprint = _fingerprint(backup_credential)
            provider_fingerprints[provider].add(backup_fingerprint)
            backup = {
                'api_base': _https_base(
                    profile.get('backup_api_base') or primary_api_base
                ),
                'credential_fingerprint': backup_fingerprint,
            }
        feature_routes.append({
            'feature': feature,
            'profile_id': profile_id,
            'provider': provider,
            'model': model,
            'primary': {
                'api_base': primary_api_base,
                'credential_fingerprint': primary_fingerprint,
            },
            'backup': backup,
        })
    if not feature_routes:
        raise RuntimeError('二区 AI feature assignments 为空')

    if config_snapshot.get('ADVENTURER_S1_MIMO_USE_DEEPSEEK_FALLBACK'):
        mimo_mode = 'region2_deepseek_fallback'
        mimo_fingerprint = _fingerprint(
            _credential(config_snapshot.get('DEEPSEEK_API_KEY'))
        )
        provider_fingerprints.setdefault('deepseek', set()).add(
            mimo_fingerprint
        )
    else:
        mimo_mode = 'dedicated_mimo'
        mimo_fingerprint = _fingerprint(
            _credential(config_snapshot.get('MIMO_API_KEY'))
        )
        _https_base(config_snapshot.get('MIMO_API_BASE'))
        if not str(config_snapshot.get('MIMO_MODEL') or '').strip():
            raise RuntimeError('二区 MiMo 模型未配置')
        provider_fingerprints.setdefault('mimo', set()).add(mimo_fingerprint)

    return {
        'ready': True,
        'credential_source': 'region2_env',
        'budget_mode': 'dedicated_provider_account_and_database_ledger',
        'budget_namespace': budget,
        'rate_limit_mode': 'runtime_process',
        'rate_limit_namespace': rate_limit,
        'feature_count': len(feature_routes),
        'feature_routes': feature_routes,
        'active_providers': sorted(provider_fingerprints),
        'assignment_digest': (
            'sha256=' + hashlib.sha256(
                _canonical_json_bytes(feature_routes)
            ).hexdigest()
        ),
        'credential_fingerprints': {
            provider: sorted(fingerprints)
            for provider, fingerprints in sorted(provider_fingerprints.items())
        },
        'mimo_mode': mimo_mode,
        'mimo_credential_fingerprint': mimo_fingerprint,
    }


def effective_ai_isolation_receipt(app) -> dict:
    try:
        return build_ai_isolation_receipt(app)
    except RuntimeError:
        if app.config.get(
            'ADVENTURER_S1_REGION_2_REQUIRE_AI_READY_FOR_ADMISSION',
            False,
        ):
            raise
        return build_degraded_ai_isolation_receipt(app)
