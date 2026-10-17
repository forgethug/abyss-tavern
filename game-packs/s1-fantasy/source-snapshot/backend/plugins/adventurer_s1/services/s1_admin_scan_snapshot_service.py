"""Short-lived, server-side snapshots for expensive S1 admin scans."""
from __future__ import annotations

import secrets
from typing import Optional

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class S1AdminScanSnapshotService:
    """Store preview payloads in the shared Redis route cache.

    Admin preview/execute pairs otherwise repeat the same historical scan.  A
    snapshot is scoped to both the operation and the admin who created it, so
    another administrator cannot execute a preview they did not request.
    """

    _KEY_PREFIX = 'adventurer_s1:admin_scan_snapshot'
    _SIGNED_PREFIX = 'signed.'
    _SIGNED_SALT = 'adventurer-s1-admin-scan-snapshot-v1'

    @classmethod
    def _cache(cls):
        return current_app.extensions.get('redis_cache')

    @classmethod
    def _key(cls, operation: str, admin_user_id: int, token: str) -> str:
        return f'{cls._KEY_PREFIX}:{operation}:{int(admin_user_id)}:{token}'

    @classmethod
    def _ttl_seconds(cls) -> int:
        configured = int(current_app.config.get(
            'ADVENTURER_S1_ADMIN_SCAN_SNAPSHOT_TTL_SECONDS', 600,
        ))
        return max(60, configured)

    @classmethod
    def _serializer(cls) -> Optional[URLSafeTimedSerializer]:
        secret_key = current_app.secret_key or current_app.config.get('JWT_SECRET_KEY')
        if not secret_key:
            return None
        return URLSafeTimedSerializer(secret_key, salt=cls._SIGNED_SALT)

    @classmethod
    def _store_signed(
        cls, operation: str, admin_user_id: int, payload: dict,
    ) -> Optional[str]:
        serializer = cls._serializer()
        if serializer is None:
            return None
        signed = serializer.dumps({
            'operation': str(operation),
            'admin_user_id': int(admin_user_id),
            'payload': payload,
        })
        return f'{cls._SIGNED_PREFIX}{signed}'

    @classmethod
    def _load_signed(
        cls, operation: str, admin_user_id: int, token_text: str,
    ) -> Optional[dict]:
        if not token_text.startswith(cls._SIGNED_PREFIX):
            return None
        serializer = cls._serializer()
        if serializer is None:
            return None
        try:
            envelope = serializer.loads(
                token_text[len(cls._SIGNED_PREFIX):],
                max_age=cls._ttl_seconds(),
            )
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get('operation') != str(operation):
            return None
        if envelope.get('admin_user_id') != int(admin_user_id):
            return None
        payload = envelope.get('payload')
        return payload if isinstance(payload, dict) else None

    @classmethod
    def store(
        cls,
        operation: str,
        admin_user_id: int,
        payload: dict,
        *,
        allow_signed_fallback: bool = False,
    ) -> Optional[str]:
        cache = cls._cache()
        if cache is None:
            return cls._store_signed(operation, admin_user_id, payload) if allow_signed_fallback else None
        token = secrets.token_urlsafe(24)
        key = cls._key(operation, admin_user_id, token)
        cache.set(key, payload, cls._ttl_seconds())
        if cache.get(key) is None:
            return cls._store_signed(operation, admin_user_id, payload) if allow_signed_fallback else None
        return token

    @classmethod
    def load(
        cls,
        operation: str,
        admin_user_id: int,
        token: object,
        *,
        allow_signed_fallback: bool = False,
    ) -> Optional[dict]:
        token_text = str(token or '').strip()
        if not token_text:
            return None
        if allow_signed_fallback and token_text.startswith(cls._SIGNED_PREFIX):
            return cls._load_signed(operation, admin_user_id, token_text)
        cache = cls._cache()
        if cache is None:
            return None
        payload = cache.get(cls._key(operation, admin_user_id, token_text))
        return payload if isinstance(payload, dict) else None

    @classmethod
    def delete(cls, operation: str, admin_user_id: int, token: object) -> None:
        token_text = str(token or '').strip()
        cache = cls._cache()
        if cache is None or not token_text:
            return
        cache.delete(cls._key(operation, admin_user_id, token_text))
