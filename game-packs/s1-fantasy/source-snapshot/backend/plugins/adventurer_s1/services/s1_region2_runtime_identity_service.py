"""Verify one central request envelope and project its minimal local identity."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import base64
import hashlib
import heapq
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from db_repository import db_manager
from db_repository.s1_runtime_identity_repository import (
    S1RuntimeIdentityRepository,
)
from models.user import User


IDENTITY_VERSION = 'v2'
ACCESS_MODE_MEMBER = 'member'
ACCESS_MODE_SPECTATOR = 'spectator'
ACCESS_MODES = frozenset({ACCESS_MODE_MEMBER, ACCESS_MODE_SPECTATOR})
IDENTITY_MAX_BYTES = 4096
IDENTITY_MAX_AGE_SECONDS = 30
_NONCE_RE = re.compile(r'\A[0-9a-f]{32}\Z', re.ASCII)
_SIGNATURE_RE = re.compile(r'\Asha256=[0-9a-f]{64}\Z', re.ASCII)
_DIGEST_RE = _SIGNATURE_RE
_ENVELOPE_KEYS = frozenset({
    'version',
    'runtime_id',
    'deployment_id',
    'realm_code',
    'access_mode',
    'user',
    'method',
    'path',
    'query_sha256',
    'body_sha256',
    'timestamp',
    'nonce',
})
_USER_KEYS = frozenset({
    'id',
    'username',
    'nickname',
    'is_admin',
    'is_active',
    'last_login_at',
})
_RUNTIME_PASSWORD_SENTINEL = '!s1-runtime-central-auth-only!'


class S1RuntimeIdentityError(ValueError):
    pass


class S1RuntimeIdentityConflict(S1RuntimeIdentityError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeIdentity:
    user: dict
    access_mode: str


class _ReplayGuard:
    _lock = threading.Lock()
    _seen: dict[str, int] = {}
    _expiry_heap: list[tuple[int, str]] = []

    @classmethod
    def consume(cls, nonce: str, *, expires_at: int, now: int) -> None:
        with cls._lock:
            while cls._expiry_heap and cls._expiry_heap[0][0] < now:
                expiry, expired_nonce = heapq.heappop(cls._expiry_heap)
                if cls._seen.get(expired_nonce) == expiry:
                    cls._seen.pop(expired_nonce, None)
            if nonce in cls._seen:
                raise S1RuntimeIdentityError('Runtime identity envelope replayed')
            cls._seen[nonce] = expires_at
            heapq.heappush(cls._expiry_heap, (expires_at, nonce))

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._seen.clear()
            cls._expiry_heap.clear()


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise S1RuntimeIdentityError('Runtime identity envelope is invalid') from exc


def _sha256_label(value: bytes) -> str:
    return 'sha256=' + hashlib.sha256(value).hexdigest()


def _decode_envelope(encoded: str) -> tuple[dict, bytes]:
    if not encoded or len(encoded) > IDENTITY_MAX_BYTES * 2:
        raise S1RuntimeIdentityError('Runtime identity envelope is invalid')
    try:
        padding = '=' * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding,
            altchars=b'-_',
            validate=True,
        )
        value = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1RuntimeIdentityError('Runtime identity envelope is invalid') from exc
    if (
        len(raw) > IDENTITY_MAX_BYTES
        or not isinstance(value, dict)
        or _canonical_json_bytes(value) != raw
    ):
        raise S1RuntimeIdentityError('Runtime identity envelope is invalid')
    return value, raw


def _validate_user(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _USER_KEYS:
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    user_id = value.get('id')
    username = value.get('username')
    nickname = value.get('nickname')
    last_login_at = value.get('last_login_at')
    if type(user_id) is not int or user_id < 1:
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    if (
        not isinstance(username, str)
        or username != username.strip()
        or not 1 <= len(username) <= 32
    ):
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    if nickname is not None and (
        not isinstance(nickname, str) or len(nickname) > 64
    ):
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    if type(value.get('is_admin')) is not bool:
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    if type(value.get('is_active')) is not bool:
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    if last_login_at is not None and (
        not isinstance(last_login_at, str) or len(last_login_at) > 64
    ):
        raise S1RuntimeIdentityError('Runtime identity user is invalid')
    return dict(value)


def verify_runtime_identity_envelope(
    request,
    *,
    secret: str,
    runtime_id: str,
    deployment_id: str,
    realm_code: str = 'region_2',
) -> VerifiedRuntimeIdentity:
    version = request.headers.get('X-S1-Identity-Version', '')
    timestamp_header = request.headers.get('X-S1-Identity-Timestamp', '')
    nonce_header = request.headers.get('X-S1-Identity-Nonce', '')
    encoded = request.headers.get('X-S1-Identity-Envelope', '')
    supplied_signature = request.headers.get('X-S1-Identity-Signature', '')
    if version != IDENTITY_VERSION:
        raise S1RuntimeIdentityError('Runtime identity version denied')
    if not timestamp_header.isascii() or not timestamp_header.isdigit():
        raise S1RuntimeIdentityError('Runtime identity timestamp denied')
    if not _NONCE_RE.fullmatch(nonce_header):
        raise S1RuntimeIdentityError('Runtime identity nonce denied')
    if not _SIGNATURE_RE.fullmatch(supplied_signature):
        raise S1RuntimeIdentityError('Runtime identity signature denied')
    if not isinstance(secret, str) or len(secret.encode('utf-8')) < 32:
        raise S1RuntimeIdentityError('Runtime identity verifier unavailable')

    envelope, raw = _decode_envelope(encoded)
    if set(envelope) != _ENVELOPE_KEYS:
        raise S1RuntimeIdentityError('Runtime identity fields denied')
    timestamp = int(timestamp_header)
    now = int(time.time())
    if abs(now - timestamp) > IDENTITY_MAX_AGE_SECONDS:
        raise S1RuntimeIdentityError('Runtime identity envelope expired')
    if (
        envelope.get('version') != version
        or envelope.get('timestamp') != timestamp
        or envelope.get('nonce') != nonce_header
    ):
        raise S1RuntimeIdentityError('Runtime identity envelope mismatch')
    if (
        envelope.get('runtime_id') != runtime_id
        or envelope.get('deployment_id') != deployment_id
        or envelope.get('realm_code') != realm_code
    ):
        raise S1RuntimeIdentityError('Runtime identity audience mismatch')
    access_mode = envelope.get('access_mode')
    if access_mode not in ACCESS_MODES:
        raise S1RuntimeIdentityError('Runtime identity access mode denied')

    signing_input = (
        f's1-runtime-identity-{version}\n{timestamp}\n{nonce_header}\n'
        f'{hashlib.sha256(raw).hexdigest()}'
    ).encode('utf-8')
    expected_signature = 'sha256=' + hmac.new(
        secret.encode('utf-8'),
        signing_input,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise S1RuntimeIdentityError('Runtime identity signature denied')
    if envelope.get('method') != request.method.upper():
        raise S1RuntimeIdentityError('Runtime identity method mismatch')
    if envelope.get('path') != request.path:
        raise S1RuntimeIdentityError('Runtime identity path mismatch')
    if not _DIGEST_RE.fullmatch(str(envelope.get('query_sha256') or '')):
        raise S1RuntimeIdentityError('Runtime identity query digest denied')
    if not _DIGEST_RE.fullmatch(str(envelope.get('body_sha256') or '')):
        raise S1RuntimeIdentityError('Runtime identity body digest denied')
    if envelope['query_sha256'] != _sha256_label(request.query_string):
        raise S1RuntimeIdentityError('Runtime identity query mismatch')
    if envelope['body_sha256'] != _sha256_label(
        request.get_data(cache=True, as_text=False)
    ):
        raise S1RuntimeIdentityError('Runtime identity body mismatch')

    user = _validate_user(envelope.get('user'))
    _ReplayGuard.consume(
        nonce_header,
        expires_at=timestamp + IDENTITY_MAX_AGE_SECONDS,
        now=now,
    )
    return VerifiedRuntimeIdentity(user=user, access_mode=access_mode)


def _projection_values(payload: dict) -> dict:
    raw_last_login = payload.get('last_login_at')
    last_login_at = None
    if raw_last_login is not None:
        try:
            last_login_at = datetime.fromisoformat(
                raw_last_login.replace('Z', '+00:00')
            ).replace(tzinfo=None)
        except ValueError as exc:
            raise S1RuntimeIdentityError(
                'Runtime identity last_login_at is invalid'
            ) from exc
    now = _portable_utc_now()
    is_active = payload['is_active']
    return {
        'id': payload['id'],
        'username': payload['username'],
        'nickname': payload.get('nickname'),
        'password_hash': _RUNTIME_PASSWORD_SENTINEL,
        'is_admin': payload['is_admin'],
        'is_frozen': not is_active,
        'is_deleted': not is_active,
        'active_title': None,
        'register_ip': None,
        'last_login_ip': None,
        'last_login_at': last_login_at,
        'created_at': now,
        'updated_at': now,
    }


def _same_projection(user: User, values: dict) -> bool:
    return all(
        getattr(user, key) == value
        for key, value in values.items()
        if key not in {'created_at', 'updated_at'}
    )


def project_runtime_identity(payload: dict) -> User:
    values = _projection_values(payload)
    existing = S1RuntimeIdentityRepository.get_by_id(values['id'])
    if existing is not None and _same_projection(existing, values):
        return existing
    try:
        S1RuntimeIdentityRepository.upsert(values)
        ok, error = db_manager.commit()
        if not ok:
            raise S1RuntimeIdentityError(
                error or 'Runtime identity projection commit failed'
            )
    except IntegrityError as exc:
        db_manager.rollback()
        raise S1RuntimeIdentityConflict(
            'Runtime identity conflicts with an existing projection'
        ) from exc
    except Exception:
        db_manager.rollback()
        raise
    user = S1RuntimeIdentityRepository.get_by_id(values['id'])
    if user is None or user.username != values['username']:
        raise S1RuntimeIdentityConflict(
            'Runtime identity conflicts with an existing projection'
        )
    return user


def build_transient_runtime_identity(payload: dict) -> User:
    return User(**_projection_values(payload))


def reset_runtime_identity_replay_guard_for_tests() -> None:
    _ReplayGuard.reset_for_tests()
