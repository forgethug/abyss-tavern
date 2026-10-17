"""Trusted transport and contracts for the isolated S1 region-2 HTTP boundary.

The caller resolves immutable membership and any read-only display target on
the central server, removes its database Session, and then passes only trusted
scalar decisions here. Nothing in this module reads a realm or access mode
from a client payload or query string.

The central gateway authenticates the public request, freezes a scalar user
snapshot, releases its database Session, and calls this bounded sender.  The
Runtime receives no central cookie/JWT and must verify the per-request HMAC
identity envelope before dispatching the synchronized S1 route.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from typing import Any
from urllib.parse import unquote, urlsplit

import jwt
import requests
from flask import current_app


REGION_2 = 'region_2'

RUNTIME_URL_CONFIG_KEY = 'ADVENTURER_S1_REGION_2_RUNTIME_URL'
RUNTIME_PROXY_SECRET_CONFIG_KEY = 'ADVENTURER_S1_REGION_2_PROXY_SECRET'
RUNTIME_BOOTSTRAP_SECRET_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_BOOTSTRAP_SECRET'
)
RUNTIME_CONNECT_TIMEOUT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_CONNECT_TIMEOUT_SECONDS'
)
RUNTIME_READ_TIMEOUT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_READ_TIMEOUT_SECONDS'
)
RUNTIME_ADMIN_READ_TIMEOUT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_ADMIN_READ_TIMEOUT_SECONDS'
)
RUNTIME_INTERACTION_READ_TIMEOUT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_INTERACTION_READ_TIMEOUT_SECONDS'
)
RUNTIME_HEALTH_READ_TIMEOUT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_HEALTH_READ_TIMEOUT_SECONDS'
)
RUNTIME_EXPECTED_RUNTIME_ID_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_RUNTIME_ID'
)
RUNTIME_EXPECTED_DEPLOYMENT_ID_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_DEPLOYMENT_ID'
)
RUNTIME_EXPECTED_CONTRACT_VERSION_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_RUNTIME_CONTRACT_VERSION'
)
RUNTIME_EXPECTED_SOURCE_COMMIT_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_SOURCE_COMMIT'
)
RUNTIME_EXPECTED_SOURCE_TREE_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_SOURCE_TREE'
)
RUNTIME_EXPECTED_TRACKED_CONTENT_DIGEST_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_TRACKED_CONTENT_SHA256'
)
RUNTIME_EXPECTED_ROUTE_DIGEST_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_REQUIRED_ROUTE_DIGEST'
)
RUNTIME_EXPECTED_MIGRATION_MANIFEST_DIGEST_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_MIGRATION_MANIFEST_SHA256'
)
RUNTIME_EXPECTED_HOST_RELEASE_DIGEST_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_RUNTIME_HOST_RELEASE_DIGEST'
)
RUNTIME_MANAGED_LOCALLY_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_RUNTIME_MANAGED_LOCALLY'
)
RUNTIME_EXPECTED_MANAGER_BOOT_ID_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_MANAGER_BOOT_ID'
)
RUNTIME_EXPECTED_SERVER_MODE_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_SERVER_MODE'
)
RUNTIME_EXPECTED_AI_BUDGET_NAMESPACE_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_AI_BUDGET_NAMESPACE'
)
RUNTIME_EXPECTED_AI_RATE_LIMIT_NAMESPACE_CONFIG_KEY = (
    'ADVENTURER_S1_REGION_2_EXPECTED_AI_RATE_LIMIT_NAMESPACE'
)

RUNTIME_PROXY_SECRET_HEADER = 'X-S1-Realm-Proxy-Secret'
RUNTIME_BOOTSTRAP_SECRET_HEADER = 'X-Runtime-Bootstrap-Secret'
RUNTIME_REALM_HEADER = 'X-S1-Realm-Code'
RUNTIME_IDENTITY_SYNC_PATH = '/api/runtime/identity/sync'
RUNTIME_HEALTH_PATH = '/api/runtime/health'
RUNTIME_READINESS_PATH = '/api/runtime/readiness/challenge'
RUNTIME_READINESS_VERSION = 'v1'
RUNTIME_CONTRACT_VERSION = 's1-runtime-v1'
RUNTIME_IDENTITY_VERSION = 'v2'
RUNTIME_ACCESS_MODE_MEMBER = 'member'
RUNTIME_ACCESS_MODE_SPECTATOR = 'spectator'
RUNTIME_ACCESS_MODES = frozenset({
    RUNTIME_ACCESS_MODE_MEMBER,
    RUNTIME_ACCESS_MODE_SPECTATOR,
})
RUNTIME_IDENTITY_VERSION_HEADER = 'X-S1-Identity-Version'
RUNTIME_IDENTITY_TIMESTAMP_HEADER = 'X-S1-Identity-Timestamp'
RUNTIME_IDENTITY_NONCE_HEADER = 'X-S1-Identity-Nonce'
RUNTIME_IDENTITY_ENVELOPE_HEADER = 'X-S1-Identity-Envelope'
RUNTIME_IDENTITY_SIGNATURE_HEADER = 'X-S1-Identity-Signature'
RUNTIME_CONTROL_VERSION = 'v1'
RUNTIME_CONTROL_VERSION_HEADER = 'X-S1-Control-Version'
RUNTIME_CONTROL_PURPOSE_HEADER = 'X-S1-Control-Purpose'
RUNTIME_CONTROL_SIGNATURE_HEADER = 'X-S1-Control-Signature'
RUNTIME_AI_CONFIG_SYNC_CONTROL_PURPOSE = 'region2-ai-config-sync'
RUNTIME_CONTROL_PURPOSES = frozenset({
    RUNTIME_AI_CONFIG_SYNC_CONTROL_PURPOSE,
})
S1_RUNTIME_PATH_PREFIX = '/api/adventurer-s1'
MAX_RUNTIME_REQUEST_BODY_BYTES = 2 * 1024 * 1024
MAX_RUNTIME_RESPONSE_BODY_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_HEALTH_BODY_BYTES = 64 * 1024
MAX_RUNTIME_READINESS_BODY_BYTES = 256 * 1024
_AMBIENT_NPC_INTERACTION_PATH_RE = re.compile(
    r'^/api/adventurer-s1/ambient-npcs/[^/]+/actions/[^/]+$'
)
_LOWER_HEX_32_RE = re.compile(r'\A[0-9a-f]{32}\Z', re.ASCII)
_LOWER_HEX_64_RE = re.compile(r'\A[0-9a-f]{64}\Z', re.ASCII)
_SOURCE_COMMIT_RE = re.compile(r'\A[0-9a-f]{40}\Z', re.ASCII)
_ROUTE_DIGEST_RE = re.compile(r'\Asha256=[0-9a-f]{64}\Z', re.ASCII)
_AI_NAMESPACE_RE = re.compile(r'\A[a-z0-9._:-]{1,100}\Z', re.ASCII)
_ALLOWED_AI_PROVIDERS = frozenset({
    'anthropic_compatible',
    'deepseek',
    'mimo',
    'openai_compatible',
    'openrouter_deepseek_v4_pro_baidu',
    'openrouter_deepseek_v4_pro_official',
})

ALLOWED_METHODS = frozenset({
    'GET',
    'HEAD',
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
    'OPTIONS',
})
FORWARDED_REQUEST_HEADERS = {
    'content-type': 'Content-Type',
    'accept': 'Accept',
    'idempotency-key': 'Idempotency-Key',
    'x-idempotency-key': 'X-Idempotency-Key',
}
FORWARDED_RESPONSE_HEADERS = {
    'retry-after': 'Retry-After',
    'x-request-id': 'X-Request-Id',
}

# Runtime traffic is an authenticated internal boundary.  Never let ambient
# provider egress proxies see its identity envelopes or control-plane secrets.
_DIRECT_RUNTIME_PROXIES = {
    'http': None,
    'https': None,
    'all': None,
}

_RUNTIME_HTTP_SESSION_POOL_EXTENSION_KEY = (
    's1_region2_runtime_http_session_pool'
)
_RUNTIME_HTTP_SESSION_POOL_INIT_LOCK = threading.Lock()


class _RuntimeHttpSessionPool:
    """Lease reusable Sessions exclusively without serializing player traffic."""

    def __init__(self, *, max_idle: int) -> None:
        self._owner_pid = os.getpid()
        self._max_idle = max(1, min(16, int(max_idle)))
        self._lock = threading.Lock()
        self._idle: list[requests.Session] = []

    def owned_by_current_process(self) -> bool:
        return self._owner_pid == os.getpid()

    def acquire(self) -> requests.Session:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        session = requests.Session()
        # Region-2 Runtime traffic is an explicit direct boundary.  Never let
        # process proxy variables or a persisted cookie alter its destination
        # or identity envelope.
        session.trust_env = False
        session.cookies.clear()
        return session

    def release(
        self,
        session: requests.Session,
        *,
        reusable: bool,
    ) -> None:
        try:
            session.cookies.clear()
        except (AttributeError, KeyError, OSError, ValueError):
            reusable = False
        if reusable:
            with self._lock:
                if len(self._idle) < self._max_idle:
                    self._idle.append(session)
                    return
        session.close()

    def close(self) -> None:
        with self._lock:
            sessions, self._idle = self._idle, []
        for session in sessions:
            session.close()


def _app_runtime_http_session_pool() -> _RuntimeHttpSessionPool:
    app = current_app._get_current_object()
    pool = app.extensions.get(_RUNTIME_HTTP_SESSION_POOL_EXTENSION_KEY)
    if (
        isinstance(pool, _RuntimeHttpSessionPool)
        and pool.owned_by_current_process()
    ):
        return pool
    try:
        max_idle = int(app.config.get(
            'ADVENTURER_S1_REGION_2_RUNTIME_WEB_THREADS',
            5,
        ))
    except (TypeError, ValueError):
        max_idle = 5
    with _RUNTIME_HTTP_SESSION_POOL_INIT_LOCK:
        pool = app.extensions.get(_RUNTIME_HTTP_SESSION_POOL_EXTENSION_KEY)
        if not (
            isinstance(pool, _RuntimeHttpSessionPool)
            and pool.owned_by_current_process()
        ):
            if isinstance(pool, _RuntimeHttpSessionPool):
                # After fork these are only copied descriptors in the child;
                # closing them cannot mutate the parent's independent pool.
                pool.close()
            pool = _RuntimeHttpSessionPool(max_idle=max_idle)
            app.extensions[_RUNTIME_HTTP_SESSION_POOL_EXTENSION_KEY] = pool
    return pool

_CENTRAL_RUNTIME_RESPONSE_HEADERS = {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'private, no-store',
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; sandbox",
}


class S1RealmRuntimeError(RuntimeError):
    """Base error for the central-to-region-2 runtime boundary."""


class S1RealmRuntimeTargetError(S1RealmRuntimeError):
    """The trusted caller did not supply a valid region-2 target."""


class S1RealmRuntimeConfigurationError(S1RealmRuntimeError):
    """Required region-2 runtime transport configuration is unavailable."""


class S1RealmRuntimeUnavailableError(S1RealmRuntimeError):
    """The region-2 runtime could not be reached within the configured limit."""


class S1RealmRuntimeIdentitySyncError(S1RealmRuntimeError):
    """The region-2 runtime rejected the central identity projection."""


class S1RealmRuntimeControlError(S1RealmRuntimeError):
    """A Runtime-only control purpose was absent or failed verification."""


class S1RealmRuntimeHealthError(S1RealmRuntimeError):
    """The configured process did not prove it is the ready region-2 runtime."""


class S1RealmRuntimeReadinessError(S1RealmRuntimeHealthError):
    """The authenticated Runtime readiness proof failed closed."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class S1RealmRuntimeResponse:
    """Transport-neutral upstream response for a thin Flask route adapter."""

    body: bytes = field(repr=False)
    status_code: int
    headers: dict[str, str] = field(repr=False)


_RUNTIME_SCHEDULER_COUNTER_FIELDS = (
    'job_count',
    'max_workers',
    'control_workers',
    'interval_jitter_seconds',
    'running_job_count',
    'peak_running_job_count',
    'completed_job_count',
    'failed_job_count',
    'slow_job_count',
    'last_duration_ms',
    'last_finished_at',
    'last_slow_duration_ms',
)
_RUNTIME_SCHEDULER_NUMBER_FIELDS = (
    'slow_job_seconds',
)
_RUNTIME_SCHEDULER_FLAG_FIELDS = (
    'initialized',
    'running',
    'last_success',
)
_RUNTIME_SCHEDULER_TASK_ID_FIELDS = (
    'last_task_id',
    'last_slow_task_id',
)
_RUNTIME_WORK_LIMIT_FIELDS = (
    'expedition_progress_batch_size',
    'expedition_progress_budget_seconds',
    'expedition_recovery_batch_size',
    'expedition_recovery_budget_seconds',
    'observer_review_recovery_batch_size',
    'observer_review_recovery_budget_seconds',
    'data_appeal_recovery_batch_size',
    'data_appeal_recovery_scan_size',
    'data_appeal_recovery_budget_seconds',
    'building_ai_recovery_batch_size',
    'building_ai_recovery_budget_seconds',
)


@dataclass(frozen=True, slots=True)
class S1RuntimePressureSnapshot:
    """Allowlisted scalar pressure counters safe for the central admin UI."""

    database_checked_out: int | None = None
    database_capacity: int | None = None
    database_overflow: int | None = None
    async_inflight_tasks: int | None = None
    async_max_workers: int | None = None
    async_queue_depth: int | None = None
    async_reserved_tasks: int | None = None
    relay_queue_depth: int | None = None
    relay_queue_capacity: int | None = None
    relay_delivery_failure_count: int | None = None
    relay_dropped_count: int | None = None
    web_threads: int | None = None
    server_backlog: int | None = None
    gameplay_capacity: int | None = None
    gameplay_inflight: int | None = None
    gameplay_peak_inflight: int | None = None
    gameplay_rejected: int | None = None
    control_thread_reserve: int | None = None
    active_requests: tuple[tuple[str, str, int, str], ...] | None = None
    scheduler: tuple[tuple[str, int | float | str | bool], ...] = ()
    work_limits: tuple[tuple[str, int], ...] = ()

    def to_admin_projection(
        self,
    ) -> dict[str, dict[str, Any]]:
        projection: dict[str, dict[str, Any]] = {}
        groups = {
            'database_pool': {
                'checked_out': self.database_checked_out,
                'capacity': self.database_capacity,
                'overflow': self.database_overflow,
            },
            'async_worker': {
                'inflight_tasks': self.async_inflight_tasks,
                'max_workers': self.async_max_workers,
                'queue_depth': self.async_queue_depth,
                'reserved_tasks': self.async_reserved_tasks,
            },
            'socket_relay': {
                'queue_depth': self.relay_queue_depth,
                'queue_capacity': self.relay_queue_capacity,
                'delivery_failure_count': self.relay_delivery_failure_count,
                'dropped_count': self.relay_dropped_count,
            },
            'server': {
                'web_threads': self.web_threads,
                'backlog': self.server_backlog,
                'gameplay_capacity': self.gameplay_capacity,
                'gameplay_inflight': self.gameplay_inflight,
                'gameplay_peak_inflight': self.gameplay_peak_inflight,
                'gameplay_rejected': self.gameplay_rejected,
                'control_thread_reserve': self.control_thread_reserve,
            },
            'scheduler': dict(self.scheduler),
            'work_limits': dict(self.work_limits),
        }
        for group_name, values in groups.items():
            present = {
                key: value
                for key, value in values.items()
                if value is not None
            }
            if present:
                projection[group_name] = present
        if self.active_requests is not None:
            projection.setdefault('server', {})['active_requests'] = [
                {'route': route, 'method': method, 'elapsed_ms': elapsed, 'location': location}
                for route, method, elapsed, location in self.active_requests
            ]
        return projection


def _runtime_pressure_from_health_data(
    data: dict[str, Any],
) -> S1RuntimePressureSnapshot | None:
    metrics = data.get('metrics')
    if not isinstance(metrics, dict):
        return None

    def _component(name: str) -> dict:
        value = metrics.get(name)
        return value if isinstance(value, dict) else {}

    def _counter(component: dict, name: str) -> int | None:
        value = component.get(name)
        return value if type(value) is int and value >= 0 else None

    def _number(component: dict, name: str) -> int | float | None:
        value = component.get(name)
        if type(value) not in {int, float} or not 0 <= value <= 3600:
            return None
        return value

    def _flag(component: dict, name: str) -> bool | None:
        value = component.get(name)
        return value if type(value) is bool else None

    def _task_id(component: dict, name: str) -> str | None:
        value = component.get(name)
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > 128:
            return None
        if not all(
            char.isascii()
            and (char.isalnum() or char in {'_', '-', ':', '.'})
            for char in value
        ):
            return None
        return value

    database_pool = _component('database_pool')
    if database_pool.get('available') is not True:
        database_pool = {}
    async_worker = _component('async_worker')
    socket_relay = _component('socket_relay')
    server = _component('server')
    scheduler = _component('scheduler')
    work_limits = _component('work_limits')
    gameplay_gate = server.get('gameplay_gate')
    if not isinstance(gameplay_gate, dict):
        gameplay_gate = {}

    # Runtime has at most 16 web threads. Keep this diagnostic projection
    # bounded and reject anything that could be a concrete URL or payload.
    active_requests = None
    raw_requests = gameplay_gate.get('active_requests')
    if isinstance(raw_requests, list) and len(raw_requests) <= 16:
        active_requests = []
        for item in raw_requests:
            if not isinstance(item, dict):
                continue
            route, method = item.get('route'), item.get('method')
            elapsed, location = item.get('elapsed_ms'), item.get('location')
            if (
                not isinstance(route, str) or len(route) > 256
                or not re.fullmatch(r'(?:/api/adventurer-s1(?:/[A-Za-z0-9_<>:.-]+)*/?|<unmatched>)', route)
                or not isinstance(method, str) or method not in ALLOWED_METHODS
                or type(elapsed) is not int or elapsed < 0
                or not isinstance(location, str) or len(location) > 160
                or (location and not re.fullmatch(r'[A-Za-z0-9_.<>-]+:[0-9]+:[A-Za-z0-9_<>]+', location))
            ):
                continue
            active_requests.append((route, method, elapsed, location))

    scheduler_projection: list[tuple[str, int | float | str | bool]] = []
    for field_name in _RUNTIME_SCHEDULER_FLAG_FIELDS:
        value = _flag(scheduler, field_name)
        if value is not None:
            scheduler_projection.append((field_name, value))
    for field_name in _RUNTIME_SCHEDULER_COUNTER_FIELDS:
        value = _counter(scheduler, field_name)
        if value is not None:
            scheduler_projection.append((field_name, value))
    for field_name in _RUNTIME_SCHEDULER_NUMBER_FIELDS:
        value = _number(scheduler, field_name)
        if value is not None:
            scheduler_projection.append((field_name, value))
    for field_name in _RUNTIME_SCHEDULER_TASK_ID_FIELDS:
        value = _task_id(scheduler, field_name)
        if value is not None:
            scheduler_projection.append((field_name, value))

    work_limits_projection: list[tuple[str, int]] = []
    for field_name in _RUNTIME_WORK_LIMIT_FIELDS:
        value = _counter(work_limits, field_name)
        if value is not None:
            work_limits_projection.append((field_name, value))

    snapshot = S1RuntimePressureSnapshot(
        database_checked_out=_counter(database_pool, 'checked_out'),
        database_capacity=_counter(database_pool, 'capacity'),
        database_overflow=_counter(database_pool, 'overflow'),
        async_inflight_tasks=_counter(async_worker, 'inflight_tasks'),
        async_max_workers=_counter(async_worker, 'max_workers'),
        async_queue_depth=_counter(async_worker, 'queue_depth'),
        async_reserved_tasks=_counter(async_worker, 'reserved_tasks'),
        relay_queue_depth=_counter(socket_relay, 'queue_depth'),
        relay_queue_capacity=_counter(socket_relay, 'queue_capacity'),
        relay_delivery_failure_count=_counter(
            socket_relay,
            'delivery_failure_count',
        ),
        relay_dropped_count=_counter(socket_relay, 'dropped_count'),
        web_threads=_counter(server, 'web_threads'),
        server_backlog=_counter(server, 'backlog'),
        gameplay_capacity=_counter(gameplay_gate, 'capacity'),
        gameplay_inflight=_counter(gameplay_gate, 'inflight'),
        gameplay_peak_inflight=_counter(gameplay_gate, 'peak_inflight'),
        gameplay_rejected=_counter(gameplay_gate, 'rejected'),
        active_requests=tuple(active_requests) if active_requests is not None else None,
        control_thread_reserve=_counter(
            gameplay_gate,
            'control_thread_reserve',
        ),
        scheduler=tuple(scheduler_projection),
        work_limits=tuple(work_limits_projection),
    )
    return snapshot if snapshot.to_admin_projection() else None


@dataclass(frozen=True, slots=True)
class S1RealmRuntimeHealth:
    """Validated health receipt used by the central admission preflight."""

    runtime_id: str
    plugin: str
    status: str
    realm_code: str
    manager_boot_id: str | None = None
    pressure: S1RuntimePressureSnapshot | None = None


@dataclass(frozen=True, slots=True)
class S1RuntimeReadinessExpectation:
    """Exact immutable release/deployment contract expected by the central host."""

    runtime_id: str
    deployment_id: str
    schema_revision: int
    runtime_contract_version: str
    source_commit: str
    source_tree: str
    tracked_content_sha256: str
    required_route_digest: str
    migration_manifest_sha256: str
    runtime_host_release_digest: str
    server_mode: str
    ai_budget_namespace: str
    ai_rate_limit_namespace: str
    central_ai_credential_fingerprints: frozenset[str] = field(repr=False)
    require_ai_ready: bool = True

    def __post_init__(self) -> None:
        for field_name in ('runtime_id', 'deployment_id'):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or any(char.isspace() or ord(char) < 32 for char in value)
            ):
                raise S1RealmRuntimeConfigurationError(
                    f'二区 Runtime {field_name} 配置无效'
                )
        if (
            isinstance(self.schema_revision, bool)
            or not isinstance(self.schema_revision, int)
            or self.schema_revision <= 0
        ):
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime schema revision 配置无效'
            )
        if self.runtime_contract_version != RUNTIME_CONTRACT_VERSION:
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime contract version 配置无效'
            )
        if (
            not isinstance(self.source_commit, str)
            or not _SOURCE_COMMIT_RE.fullmatch(self.source_commit)
        ):
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime source commit 配置无效'
            )
        if (
            not isinstance(self.source_tree, str)
            or not _SOURCE_COMMIT_RE.fullmatch(self.source_tree)
        ):
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime source tree 配置无效'
            )
        for field_name in (
            'tracked_content_sha256',
            'migration_manifest_sha256',
            'runtime_host_release_digest',
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _LOWER_HEX_64_RE.fullmatch(value):
                raise S1RealmRuntimeConfigurationError(
                    f'二区 Runtime {field_name} 配置无效'
                )
        if (
            not isinstance(self.required_route_digest, str)
            or not _ROUTE_DIGEST_RE.fullmatch(self.required_route_digest)
        ):
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime route digest 配置无效'
            )
        if self.server_mode not in {'development', 'gunicorn'}:
            raise S1RealmRuntimeConfigurationError(
                '二区 Runtime server mode 配置无效'
            )
        for field_name in ('ai_budget_namespace', 'ai_rate_limit_namespace'):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _AI_NAMESPACE_RE.fullmatch(value):
                raise S1RealmRuntimeConfigurationError(
                    f'二区 Runtime {field_name} 配置无效'
                )
        fingerprints = self.central_ai_credential_fingerprints
        if not isinstance(fingerprints, frozenset) or any(
            not isinstance(value, str) or not _ROUTE_DIGEST_RE.fullmatch(value)
            for value in fingerprints
        ):
            raise S1RealmRuntimeConfigurationError(
                '中央 AI 凭据指纹配置无效'
            )
        if type(self.require_ai_ready) is not bool:
            raise S1RealmRuntimeConfigurationError(
                'region-2 AI readiness policy is invalid'
            )


@dataclass(frozen=True, slots=True, repr=False)
class S1RuntimeReadinessChallenge:
    """Single-request challenge; headers/body contain internal credentials."""

    challenge_id: str
    nonce: str
    expectation: S1RuntimeReadinessExpectation
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class S1RuntimeReadinessReceipt:
    """Non-secret receipt accepted after a trusted Runtime readiness check."""

    runtime_id: str
    realm_code: str
    deployment_id: str
    schema_revision: int
    runtime_contract_version: str
    source_commit: str
    source_tree: str
    tracked_content_sha256: str
    required_route_digest: str
    migration_manifest_sha256: str
    runtime_host_release_digest: str
    server_mode: str
    queue_isolation_mode: str
    cache_isolation_mode: str
    pressure: S1RuntimePressureSnapshot | None = None

    @property
    def status(self) -> str:
        return 'ready'


def _build_readiness_receipt(
    expectation: S1RuntimeReadinessExpectation,
    *,
    queue_isolation_mode: str,
    pressure: S1RuntimePressureSnapshot | None = None,
) -> S1RuntimeReadinessReceipt:
    if queue_isolation_mode not in {'process_local', 'redis_namespace'}:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 异步队列隔离证明无效'
        )
    return S1RuntimeReadinessReceipt(
        runtime_id=expectation.runtime_id,
        realm_code=REGION_2,
        deployment_id=expectation.deployment_id,
        schema_revision=expectation.schema_revision,
        runtime_contract_version=expectation.runtime_contract_version,
        source_commit=expectation.source_commit,
        source_tree=expectation.source_tree,
        tracked_content_sha256=expectation.tracked_content_sha256,
        required_route_digest=expectation.required_route_digest,
        migration_manifest_sha256=expectation.migration_manifest_sha256,
        runtime_host_release_digest=expectation.runtime_host_release_digest,
        server_mode=expectation.server_mode,
        queue_isolation_mode=queue_isolation_mode,
        cache_isolation_mode='process_local',
        pressure=pressure,
    )


@dataclass(frozen=True, slots=True, repr=False)
class S1RuntimeIdentitySnapshot:
    """Scalar central identity projection safe across Session removal."""

    user_id: int
    username: str
    nickname: str | None
    is_admin: bool
    is_frozen: bool
    is_deleted: bool
    last_login_at: datetime | None

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int):
            raise TypeError('user_id 必须是整数')
        if self.user_id <= 0:
            raise ValueError('user_id 必须为正整数')
        if not isinstance(self.username, str):
            raise TypeError('username 必须是字符串')
        username = self.username.strip()
        if not username or len(username) > 32:
            raise ValueError('username 无效')
        object.__setattr__(self, 'username', username)

        if self.nickname is not None and not isinstance(self.nickname, str):
            raise TypeError('nickname 必须是字符串或 None')
        nickname = self.nickname.strip() if self.nickname is not None else None
        if nickname and len(nickname) > 64:
            raise ValueError('nickname 过长')
        object.__setattr__(self, 'nickname', nickname or None)

        for field_name in ('is_admin', 'is_frozen', 'is_deleted'):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f'{field_name} 必须是布尔值')
        if self.last_login_at is not None and not isinstance(
            self.last_login_at,
            datetime,
        ):
            raise TypeError('last_login_at 必须是 datetime 或 None')

    def to_runtime_payload(self) -> dict[str, Any]:
        return {
            'id': self.user_id,
            'username': self.username,
            'nickname': self.nickname,
            'is_admin': self.is_admin,
            'is_active': not (self.is_frozen or self.is_deleted),
            'last_login_at': datetime_to_utc_iso(self.last_login_at),
        }


@dataclass(frozen=True, slots=True)
class S1ValidatedRuntimeRequest:
    """Validated client request fields before secret injection or HTTP I/O."""

    method: str
    path: str
    query_string: str
    body: bytes | None = field(repr=False)
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class S1RuntimeIdentityEnvelope:
    """Scoped Runtime-only identity proof; never contains a central JWT."""

    timestamp: int
    nonce: str
    envelope: dict[str, Any]
    headers: dict[str, str]


def datetime_to_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return f'{value.isoformat()}Z'
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace('+00:00', 'Z')
    )


def require_region_2(resolved_realm_code: str) -> None:
    """Accept only a scalar region-2 routing decision made by the gateway."""
    if resolved_realm_code != REGION_2:
        raise S1RealmRuntimeTargetError('该请求不属于二区 Runtime')


def validate_runtime_url(raw_value: object) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise S1RealmRuntimeConfigurationError('二区 Runtime 地址未配置')
    value = raw_value.strip().rstrip('/')
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise S1RealmRuntimeConfigurationError('二区 Runtime 地址配置无效')
    if (
        parsed.scheme == 'http'
        and parsed.hostname not in {'127.0.0.1', '::1', 'localhost'}
    ):
        raise S1RealmRuntimeConfigurationError(
            '非本机二区 Runtime 必须使用 TLS'
        )
    return value


def validate_secret(
    raw_value: object,
    *,
    config_key: str,
    minimum_length: int = 32,
) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise S1RealmRuntimeConfigurationError(
            f'二区 Runtime 密钥配置缺失：{config_key}'
        )
    if '\r' in raw_value or '\n' in raw_value:
        raise S1RealmRuntimeConfigurationError(
            f'二区 Runtime 密钥配置无效：{config_key}'
        )
    if len(raw_value.encode('utf-8')) < minimum_length:
        raise S1RealmRuntimeConfigurationError(
            f'二区 Runtime 密钥配置长度不足：{config_key}'
        )
    return raw_value


def _validate_lower_hex(
    value: object,
    *,
    pattern: re.Pattern[str],
    field_name: str,
) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise S1RealmRuntimeReadinessError(
            f'二区 Runtime readiness {field_name} 无效'
        )
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness 响应无法规范化'
        ) from exc


def _readiness_proof(data: object, bootstrap_secret: str) -> str:
    data_digest = hashlib.sha256(_canonical_json_bytes(data)).hexdigest()
    signing_string = f's1-runtime-readiness-v1\n{data_digest}'.encode('utf-8')
    signature = hmac.new(
        bootstrap_secret.encode('utf-8'),
        signing_string,
        hashlib.sha256,
    ).hexdigest()
    return f'sha256={signature}'


def _ai_secret_fingerprints(secret: object) -> set[str]:
    """Return only digests for one locally available central credential."""
    if secret in (None, ''):
        return set()
    if not isinstance(secret, str):
        raise S1RealmRuntimeConfigurationError(
            '中央 AI 凭据配置无效'
        )
    candidates = {secret}
    stripped = secret.strip()
    if stripped:
        candidates.add(stripped)
    return {
        f'sha256={hashlib.sha256(value.encode("utf-8")).hexdigest()}'
        for value in candidates
        if value
    }


def _central_ai_credential_fingerprints(
    config: Mapping[str, Any],
) -> frozenset[str]:
    """Fingerprint central/region-1 AI credentials without exposing a secret."""
    fingerprints: set[str] = set()
    for config_key in ('DEEPSEEK_API_KEY', 'MIMO_API_KEY'):
        fingerprints.update(_ai_secret_fingerprints(config.get(config_key)))

    raw_profiles = config.get('ADVENTURER_S1_AI_PROFILES_JSON')
    if raw_profiles in (None, ''):
        profiles: object = []
    elif isinstance(raw_profiles, list):
        profiles = raw_profiles
    elif isinstance(raw_profiles, str):
        try:
            profiles = json.loads(raw_profiles)
        except (TypeError, ValueError) as exc:
            raise S1RealmRuntimeConfigurationError(
                '中央 S1 AI profile 配置无效'
            ) from exc
    else:
        raise S1RealmRuntimeConfigurationError(
            '中央 S1 AI profile 配置无效'
        )
    if not isinstance(profiles, list):
        raise S1RealmRuntimeConfigurationError(
            '中央 S1 AI profile 配置无效'
        )
    for profile in profiles:
        if not isinstance(profile, dict):
            raise S1RealmRuntimeConfigurationError(
                '中央 S1 AI profile 配置无效'
            )
        provider = str(profile.get('provider') or 'openai_compatible').strip()
        if provider not in _ALLOWED_AI_PROVIDERS:
            provider = 'openai_compatible'
        for field_name in ('api_key', 'backup_api_key'):
            fingerprints.update(
                _ai_secret_fingerprints(profile.get(field_name))
            )
    return frozenset(fingerprints)


def _validate_ai_isolation_component(
    component: object,
    *,
    expectation: S1RuntimeReadinessExpectation,
) -> dict[str, Any]:
    exact_keys = {
        'ready',
        'credential_source',
        'budget_mode',
        'budget_namespace',
        'rate_limit_mode',
        'rate_limit_namespace',
        'feature_count',
        'feature_routes',
        'active_providers',
        'assignment_digest',
        'credential_fingerprints',
        'mimo_mode',
        'mimo_credential_fingerprint',
    }
    if not isinstance(component, dict) or set(component) != exact_keys:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI 隔离证明结构无效'
        )
    if component.get('ready') is False:
        degraded_receipt = {
            'ready': False,
            'credential_source': 'region2_unconfigured',
            'budget_mode': 'dedicated_provider_account_and_database_ledger',
            'budget_namespace': expectation.ai_budget_namespace,
            'rate_limit_mode': 'runtime_process',
            'rate_limit_namespace': expectation.ai_rate_limit_namespace,
            'feature_count': 0,
            'feature_routes': [],
            'active_providers': [],
            'assignment_digest': (
                'sha256=' + hashlib.sha256(
                    _canonical_json_bytes([])
                ).hexdigest()
            ),
            'credential_fingerprints': {},
            'mimo_mode': 'unconfigured',
            'mimo_credential_fingerprint': None,
        }
        if expectation.require_ai_ready or component != degraded_receipt:
            raise S1RealmRuntimeReadinessError(
                'region-2 Runtime AI is not independently configured'
            )
        return component
    if (
        component.get('ready') is not True
        or component.get('credential_source') != 'region2_env'
        or component.get('budget_mode')
        != 'dedicated_provider_account_and_database_ledger'
        or component.get('budget_namespace')
        != expectation.ai_budget_namespace
        or component.get('rate_limit_mode') != 'runtime_process'
        or component.get('rate_limit_namespace')
        != expectation.ai_rate_limit_namespace
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI 预算或限流隔离证明无效'
        )

    feature_count = component.get('feature_count')
    feature_routes = component.get('feature_routes')
    assignment_digest = component.get('assignment_digest')
    if (
        type(feature_count) is not int
        or feature_count < 1
        or not isinstance(feature_routes, list)
        or len(feature_routes) != feature_count
        or not isinstance(assignment_digest, str)
        or not _ROUTE_DIGEST_RE.fullmatch(assignment_digest)
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI 功能分配证明无效'
        )

    route_features: list[str] = []
    recomputed_fingerprints: dict[str, set[str]] = {}
    for route in feature_routes:
        if not isinstance(route, dict) or set(route) != {
            'feature',
            'profile_id',
            'provider',
            'model',
            'primary',
            'backup',
        }:
            raise S1RealmRuntimeReadinessError(
                '二区 Runtime AI feature route 结构无效'
            )
        feature = route.get('feature')
        profile_id = route.get('profile_id')
        provider = route.get('provider')
        model = route.get('model')
        if (
            not isinstance(feature, str)
            or not re.fullmatch(r'[a-z0-9_:-]{1,100}', feature)
            or not isinstance(profile_id, str)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', profile_id)
            or provider not in _ALLOWED_AI_PROVIDERS
            or not isinstance(model, str)
            or not model.strip()
            or model != model.strip()
        ):
            raise S1RealmRuntimeReadinessError(
                '二区 Runtime AI feature route 值无效'
            )
        route_features.append(feature)
        primary_fingerprint: str | None = None
        for index, slot in enumerate((route.get('primary'), route.get('backup'))):
            if slot is None:
                if index == 0:
                    raise S1RealmRuntimeReadinessError(
                        '二区 Runtime AI primary route 缺失'
                    )
                continue
            if not isinstance(slot, dict) or set(slot) != {
                'api_base',
                'credential_fingerprint',
            }:
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime AI route slot 结构无效'
                )
            api_base = slot.get('api_base')
            try:
                parsed_api_base = urlsplit(str(api_base or ''))
                parsed_api_base.port
            except ValueError as exc:
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime AI api_base 无效'
                ) from exc
            fingerprint = slot.get('credential_fingerprint')
            if (
                not isinstance(api_base, str)
                or api_base != api_base.strip()
                or parsed_api_base.scheme != 'https'
                or not parsed_api_base.hostname
                or parsed_api_base.username is not None
                or parsed_api_base.password is not None
                or bool(parsed_api_base.query)
                or bool(parsed_api_base.fragment)
                or not isinstance(fingerprint, str)
                or not _ROUTE_DIGEST_RE.fullmatch(fingerprint)
            ):
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime AI route slot 值无效'
                )
            if index == 0:
                primary_fingerprint = fingerprint
            elif fingerprint == primary_fingerprint:
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime AI backup route 未提供独立凭据'
                )
            recomputed_fingerprints.setdefault(provider, set()).add(
                fingerprint
            )
    if route_features != sorted(set(route_features)):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI feature routes 未稳定排序或存在重复'
        )
    expected_assignment_digest = (
        'sha256='
        + hashlib.sha256(_canonical_json_bytes(feature_routes)).hexdigest()
    )
    if not hmac.compare_digest(assignment_digest, expected_assignment_digest):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI 功能分配摘要无效'
        )

    mimo_mode = component.get('mimo_mode')
    mimo_fingerprint = component.get('mimo_credential_fingerprint')
    if (
        not isinstance(mimo_fingerprint, str)
        or not _ROUTE_DIGEST_RE.fullmatch(mimo_fingerprint)
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime MiMo 凭据证明无效'
        )
    if mimo_mode == 'dedicated_mimo':
        recomputed_fingerprints.setdefault('mimo', set()).add(
            mimo_fingerprint
        )
    elif mimo_mode == 'region2_deepseek_fallback':
        recomputed_fingerprints.setdefault('deepseek', set()).add(
            mimo_fingerprint
        )
    else:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime MiMo 路由证明无效'
        )

    expected_fingerprints = {
        provider: sorted(values)
        for provider, values in sorted(recomputed_fingerprints.items())
    }
    expected_providers = sorted(expected_fingerprints)
    if (
        component.get('active_providers') != expected_providers
        or component.get('credential_fingerprints')
        != expected_fingerprints
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime AI provider 聚合证明与 feature routes 不一致'
        )
    flattened = {
        fingerprint
        for values in expected_fingerprints.values()
        for fingerprint in values
    }
    if flattened & expectation.central_ai_credential_fingerprints:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 复用了中央或一区 AI 凭据'
        )
    return component


def build_readiness_challenge(
    *,
    expectation: S1RuntimeReadinessExpectation,
    proxy_secret: object,
    bootstrap_secret: object,
    issued_at: int | None = None,
    challenge_id: str | None = None,
    nonce: str | None = None,
    jwt_id: str | None = None,
) -> S1RuntimeReadinessChallenge:
    """Build, but do not send, a short-lived authenticated readiness challenge."""
    if not isinstance(expectation, S1RuntimeReadinessExpectation):
        raise S1RealmRuntimeConfigurationError(
            '二区 Runtime readiness expectation 无效'
        )
    proxy_secret_value = validate_secret(
        proxy_secret,
        config_key=RUNTIME_PROXY_SECRET_CONFIG_KEY,
    )
    bootstrap_secret_value = validate_secret(
        bootstrap_secret,
        config_key=RUNTIME_BOOTSTRAP_SECRET_CONFIG_KEY,
    )
    if proxy_secret_value == bootstrap_secret_value:
        raise S1RealmRuntimeConfigurationError(
            '二区 Runtime 内部密钥必须相互独立'
        )
    if issued_at is None:
        issued_at = int(datetime.now(timezone.utc).timestamp())
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        raise S1RealmRuntimeConfigurationError(
            '二区 Runtime readiness 签发时间无效'
        )
    challenge_id_value = challenge_id or secrets.token_hex(16)
    nonce_value = nonce or secrets.token_hex(32)
    jwt_id_value = jwt_id or secrets.token_hex(16)
    _validate_lower_hex(
        challenge_id_value,
        pattern=_LOWER_HEX_32_RE,
        field_name='challenge_id',
    )
    _validate_lower_hex(
        nonce_value,
        pattern=_LOWER_HEX_64_RE,
        field_name='nonce',
    )
    _validate_lower_hex(
        jwt_id_value,
        pattern=_LOWER_HEX_32_RE,
        field_name='jti',
    )

    expected = {
        'expected_runtime_id': expectation.runtime_id,
        'expected_deployment_id': expectation.deployment_id,
        'expected_schema_revision': expectation.schema_revision,
        'expected_runtime_contract_version': (
            expectation.runtime_contract_version
        ),
        'expected_source_commit': expectation.source_commit,
        'expected_source_tree': expectation.source_tree,
        'expected_tracked_content_sha256': (
            expectation.tracked_content_sha256
        ),
        'expected_required_route_digest': expectation.required_route_digest,
        'expected_migration_manifest_sha256': (
            expectation.migration_manifest_sha256
        ),
        'expected_runtime_host_release_digest': (
            expectation.runtime_host_release_digest
        ),
        'expected_server_mode': expectation.server_mode,
    }
    claims = {
        'purpose': 's1_runtime_readiness',
        'challenge_id': challenge_id_value,
        'nonce': nonce_value,
        'realm_code': REGION_2,
        **expected,
        'iat': issued_at,
        'exp': issued_at + 60,
        'jti': jwt_id_value,
    }
    jwt_challenge = jwt.encode(
        claims,
        bootstrap_secret_value,
        algorithm='HS256',
    )
    body = {
        'version': RUNTIME_READINESS_VERSION,
        'challenge_id': challenge_id_value,
        'nonce': nonce_value,
        **expected,
        'jwt_challenge': jwt_challenge,
    }
    return S1RuntimeReadinessChallenge(
        challenge_id=challenge_id_value,
        nonce=nonce_value,
        expectation=expectation,
        headers={
            'Content-Type': 'application/json',
            RUNTIME_REALM_HEADER: REGION_2,
            RUNTIME_PROXY_SECRET_HEADER: proxy_secret_value,
            RUNTIME_BOOTSTRAP_SECRET_HEADER: bootstrap_secret_value,
        },
        body=body,
    )


def validate_readiness_response(
    *,
    payload: object,
    challenge: S1RuntimeReadinessChallenge,
    bootstrap_secret: object,
) -> S1RuntimeReadinessReceipt:
    """Validate one Runtime response against the exact pending challenge."""
    if not isinstance(challenge, S1RuntimeReadinessChallenge):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness challenge 无效'
        )
    bootstrap_secret_value = validate_secret(
        bootstrap_secret,
        config_key=RUNTIME_BOOTSTRAP_SECRET_CONFIG_KEY,
    )
    if not isinstance(payload, dict) or set(payload) != {
        'code',
        'message',
        'data',
        'proof',
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness 响应结构无效'
        )
    data = payload.get('data')
    proof = payload.get('proof')
    if (
        payload.get('code') != 200
        or payload.get('message') != 'ready'
        or not isinstance(data, dict)
        or not isinstance(proof, str)
        or not hmac.compare_digest(
            _readiness_proof(data, bootstrap_secret_value),
            proof,
        )
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness 验签失败'
        )
    required_data_fields = {
        'version',
        'status',
        'challenge_id',
        'nonce',
        'runtime_id',
        'realm_code',
        'deployment_id',
        'schema_revision',
        'runtime_contract_version',
        'source_commit',
        'source_tree',
        'tracked_content_sha256',
        'required_route_digest',
        'migration_manifest_sha256',
        'runtime_host_release_digest',
        'components',
    }
    if set(data) not in (
        required_data_fields,
        required_data_fields | {'metrics'},
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness data 结构无效'
        )
    if 'metrics' in data and not isinstance(data.get('metrics'), dict):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness metrics 结构无效'
        )
    expectation = challenge.expectation
    expected_scalars = {
        'version': RUNTIME_READINESS_VERSION,
        'status': 'ready',
        'challenge_id': challenge.challenge_id,
        'nonce': challenge.nonce,
        'runtime_id': expectation.runtime_id,
        'realm_code': REGION_2,
        'deployment_id': expectation.deployment_id,
        'schema_revision': expectation.schema_revision,
        'runtime_contract_version': expectation.runtime_contract_version,
        'source_commit': expectation.source_commit,
        'source_tree': expectation.source_tree,
        'tracked_content_sha256': expectation.tracked_content_sha256,
        'required_route_digest': expectation.required_route_digest,
        'migration_manifest_sha256': expectation.migration_manifest_sha256,
        'runtime_host_release_digest': expectation.runtime_host_release_digest,
    }
    if any(data.get(key) != value for key, value in expected_scalars.items()):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness 与中央预期不一致'
        )

    components = data.get('components')
    if not isinstance(components, dict) or set(components) != {
        'database_identity',
        'process_lease',
        'scheduler',
        'async_worker',
        'socket_relay',
        'active_slot_authority',
        'queue',
        'cache',
        'realm_adapter',
        'ai_isolation',
        'server',
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime readiness 组件结构无效'
        )
    if components.get('database_identity') != {
        'ready': True,
        'source': 'database',
        'dialect': 'postgresql',
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 数据库部署身份未就绪'
        )
    if components.get('process_lease') != {
        'enabled': True,
        'alive': True,
        'mode': 'postgresql_session_advisory_lock',
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 进程租约未就绪'
        )
    for component_name in ('scheduler', 'async_worker'):
        if components.get(component_name) != {
            'enabled': True,
            'alive': True,
        }:
            raise S1RealmRuntimeReadinessError(
                f'二区 Runtime {component_name} 未就绪'
            )
    if components.get('socket_relay') != {
        'enabled': True,
        'alive': True,
        'probe_enabled': True,
        'transport_verified': True,
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime socket relay 未完成探针验证'
        )
    if components.get('active_slot_authority') != {
        'enabled': True,
        'reconciled': True,
        'realm_code': REGION_2,
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 尚未与中央唯一存活角色槽位完成对账'
        )
    if components.get('queue') not in (
        {'isolation_mode': 'process_local'},
        {'isolation_mode': 'redis_namespace'},
    ):
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 异步队列隔离证明无效'
        )
    if components.get('cache') != {'isolation_mode': 'process_local'}:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 缓存隔离证明无效'
        )
    if components.get('server') != {
        'mode': expectation.server_mode,
        'worker_model': (
            'single_gthread'
            if expectation.server_mode == 'gunicorn'
            else 'single_development'
        ),
        'worker_count': 1,
        'observed': True,
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 生产服务模式证明无效'
        )
    if components.get('realm_adapter') != {
        'gateway_enabled': False,
        'fixed_realm_code': REGION_2,
    }:
        raise S1RealmRuntimeReadinessError(
            '二区 Runtime 固定区服适配器证明无效'
        )
    _validate_ai_isolation_component(
        components.get('ai_isolation'),
        expectation=expectation,
    )
    return _build_readiness_receipt(
        expectation,
        queue_isolation_mode=components['queue']['isolation_mode'],
        pressure=_runtime_pressure_from_health_data(data),
    )


def validate_timeout(raw_value: object) -> float:
    if isinstance(raw_value, bool):
        raise S1RealmRuntimeConfigurationError('二区 Runtime 超时配置无效')
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise S1RealmRuntimeConfigurationError(
            '二区 Runtime 超时配置无效'
        ) from exc
    if not 0 < timeout <= 900:
        raise S1RealmRuntimeConfigurationError('二区 Runtime 超时配置无效')
    return timeout


def validate_runtime_path(path: str) -> str:
    if not isinstance(path, str):
        raise S1RealmRuntimeTargetError('二区 Runtime 路径必须是字符串')
    if not path.startswith('/') or path.startswith('//'):
        raise S1RealmRuntimeTargetError('二区 Runtime 路径无效')
    if '\\' in path or '\r' in path or '\n' in path:
        raise S1RealmRuntimeTargetError('二区 Runtime 路径无效')
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise S1RealmRuntimeTargetError('二区 Runtime 路径不得包含地址或查询串')
    decoded_path = unquote(parsed.path)
    if any(part in {'.', '..'} for part in decoded_path.split('/')):
        raise S1RealmRuntimeTargetError('二区 Runtime 路径不得包含相对段')
    if not (
        parsed.path == S1_RUNTIME_PATH_PREFIX
        or parsed.path.startswith(f'{S1_RUNTIME_PATH_PREFIX}/')
    ):
        raise S1RealmRuntimeTargetError('二区 Runtime 路径超出 S1 范围')
    return parsed.path


def normalize_query_string(query_string: bytes | str | None) -> str:
    if query_string is None:
        return ''
    if isinstance(query_string, bytes):
        value = query_string.decode('latin-1')
    elif isinstance(query_string, str):
        value = query_string
    else:
        raise S1RealmRuntimeTargetError('二区 Runtime 查询串类型无效')
    if value.startswith('?') or '#' in value or '\r' in value or '\n' in value:
        raise S1RealmRuntimeTargetError('二区 Runtime 查询串无效')
    return value


def normalize_method(method: str) -> str:
    if not isinstance(method, str):
        raise S1RealmRuntimeTargetError('二区 Runtime 请求方法无效')
    normalized = method.strip().upper()
    if normalized not in ALLOWED_METHODS:
        raise S1RealmRuntimeTargetError('二区 Runtime 请求方法不受支持')
    return normalized


def normalize_runtime_access_mode(access_mode: str) -> str:
    if access_mode not in RUNTIME_ACCESS_MODES:
        raise S1RealmRuntimeTargetError('二区 Runtime 访问模式无效')
    return access_mode


def normalize_body(
    body: bytes | bytearray | memoryview | None,
) -> bytes | None:
    if body is None:
        return None
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise S1RealmRuntimeTargetError('二区 Runtime 请求体必须是字节串')
    normalized = bytes(body)
    if len(normalized) > MAX_RUNTIME_REQUEST_BODY_BYTES:
        raise S1RealmRuntimeTargetError('二区 Runtime 请求体超过安全上限')
    return normalized


def _sha256_label(value: bytes) -> str:
    return f'sha256={hashlib.sha256(value).hexdigest()}'


def build_identity_envelope(
    *,
    identity: S1RuntimeIdentitySnapshot,
    expectation: S1RuntimeReadinessExpectation,
    method: str,
    path: str,
    query_string: bytes | str | None,
    body: bytes | bytearray | memoryview | None,
    proxy_secret: object,
    access_mode: str = RUNTIME_ACCESS_MODE_MEMBER,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> S1RuntimeIdentityEnvelope:
    """Bind a central-authenticated user snapshot to one exact Runtime request."""
    if not isinstance(identity, S1RuntimeIdentitySnapshot):
        raise S1RealmRuntimeTargetError('二区 Runtime 身份快照无效')
    if not isinstance(expectation, S1RuntimeReadinessExpectation):
        raise S1RealmRuntimeTargetError('二区 Runtime 身份受众无效')
    method_value = normalize_method(method)
    access_mode_value = normalize_runtime_access_mode(access_mode)
    path_value = validate_runtime_path(path)
    query_value = normalize_query_string(query_string)
    try:
        query_bytes = query_value.encode('latin-1')
    except UnicodeEncodeError as exc:
        raise S1RealmRuntimeTargetError(
            '二区 Runtime 查询串必须保持原始字节'
        ) from exc
    body_value = normalize_body(body) or b''
    proxy_secret_value = validate_secret(
        proxy_secret,
        config_key=RUNTIME_PROXY_SECRET_CONFIG_KEY,
    )
    if timestamp is None:
        timestamp = int(datetime.now(timezone.utc).timestamp())
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise S1RealmRuntimeTargetError('二区 Runtime 身份时间戳无效')
    nonce_value = nonce or secrets.token_hex(16)
    _validate_lower_hex(
        nonce_value,
        pattern=_LOWER_HEX_32_RE,
        field_name='identity nonce',
    )
    envelope = {
        'version': RUNTIME_IDENTITY_VERSION,
        'runtime_id': expectation.runtime_id,
        'deployment_id': expectation.deployment_id,
        'realm_code': REGION_2,
        'access_mode': access_mode_value,
        'user': identity.to_runtime_payload(),
        'method': method_value,
        'path': path_value,
        'query_sha256': _sha256_label(query_bytes),
        'body_sha256': _sha256_label(body_value),
        'timestamp': timestamp,
        'nonce': nonce_value,
    }
    canonical = _canonical_json_bytes(envelope)
    envelope_token = base64.urlsafe_b64encode(canonical).rstrip(b'=').decode('ascii')
    signing_string = (
        f's1-runtime-identity-v2\n{timestamp}\n{nonce_value}\n'
        f'{hashlib.sha256(canonical).hexdigest()}'
    ).encode('utf-8')
    signature = hmac.new(
        proxy_secret_value.encode('utf-8'),
        signing_string,
        hashlib.sha256,
    ).hexdigest()
    return S1RuntimeIdentityEnvelope(
        timestamp=timestamp,
        nonce=nonce_value,
        envelope=envelope,
        headers={
            RUNTIME_IDENTITY_VERSION_HEADER: RUNTIME_IDENTITY_VERSION,
            RUNTIME_IDENTITY_TIMESTAMP_HEADER: str(timestamp),
            RUNTIME_IDENTITY_NONCE_HEADER: nonce_value,
            RUNTIME_IDENTITY_ENVELOPE_HEADER: envelope_token,
            RUNTIME_IDENTITY_SIGNATURE_HEADER: f'sha256={signature}',
        },
    )


def _runtime_control_signing_bytes(
    *,
    purpose: str,
    timestamp: str,
    nonce: str,
    encoded_identity_envelope: str,
) -> bytes:
    return (
        f's1-runtime-control-v1\n{purpose}\n{timestamp}\n{nonce}\n'
        f'{hashlib.sha256(encoded_identity_envelope.encode("ascii")).hexdigest()}'
    ).encode('utf-8')


def build_runtime_control_headers(
    *,
    purpose: str,
    identity_envelope: S1RuntimeIdentityEnvelope,
    proxy_secret: object,
) -> dict[str, str]:
    """Mint one non-forwardable control grant bound to an identity request."""
    if purpose not in RUNTIME_CONTROL_PURPOSES:
        raise S1RealmRuntimeTargetError('二区 Runtime 控制用途无效')
    if not isinstance(identity_envelope, S1RuntimeIdentityEnvelope):
        raise S1RealmRuntimeTargetError('二区 Runtime 控制身份无效')
    proxy_secret_value = validate_secret(
        proxy_secret,
        config_key=RUNTIME_PROXY_SECRET_CONFIG_KEY,
    )
    encoded = identity_envelope.headers[RUNTIME_IDENTITY_ENVELOPE_HEADER]
    signing_bytes = _runtime_control_signing_bytes(
        purpose=purpose,
        timestamp=str(identity_envelope.timestamp),
        nonce=identity_envelope.nonce,
        encoded_identity_envelope=encoded,
    )
    signature = hmac.new(
        proxy_secret_value.encode('utf-8'),
        signing_bytes,
        hashlib.sha256,
    ).hexdigest()
    return {
        RUNTIME_CONTROL_VERSION_HEADER: RUNTIME_CONTROL_VERSION,
        RUNTIME_CONTROL_PURPOSE_HEADER: purpose,
        RUNTIME_CONTROL_SIGNATURE_HEADER: f'sha256={signature}',
    }


def verify_runtime_control_headers(
    headers: Mapping[str, str],
    *,
    expected_purpose: str,
    proxy_secret: object,
) -> None:
    """Verify a server-minted control grant after identity verification."""
    if expected_purpose not in RUNTIME_CONTROL_PURPOSES:
        raise S1RealmRuntimeControlError('二区 Runtime 控制用途无效')
    getter = getattr(headers, 'get', None)
    if not callable(getter):
        raise S1RealmRuntimeControlError('二区 Runtime 控制请求无效')
    version = str(getter(RUNTIME_CONTROL_VERSION_HEADER) or '')
    purpose = str(getter(RUNTIME_CONTROL_PURPOSE_HEADER) or '')
    timestamp = str(getter(RUNTIME_IDENTITY_TIMESTAMP_HEADER) or '')
    nonce = str(getter(RUNTIME_IDENTITY_NONCE_HEADER) or '')
    encoded = str(getter(RUNTIME_IDENTITY_ENVELOPE_HEADER) or '')
    supplied_signature = str(
        getter(RUNTIME_CONTROL_SIGNATURE_HEADER) or ''
    )
    if (
        version != RUNTIME_CONTROL_VERSION
        or purpose != expected_purpose
        or not timestamp.isdigit()
        or not _LOWER_HEX_32_RE.fullmatch(nonce)
        or not encoded
        or not encoded.isascii()
        or not _ROUTE_DIGEST_RE.fullmatch(supplied_signature)
    ):
        raise S1RealmRuntimeControlError('二区 Runtime 控制授权失败')
    proxy_secret_value = validate_secret(
        proxy_secret,
        config_key=RUNTIME_PROXY_SECRET_CONFIG_KEY,
    )
    expected_signature = 'sha256=' + hmac.new(
        proxy_secret_value.encode('utf-8'),
        _runtime_control_signing_bytes(
            purpose=purpose,
            timestamp=timestamp,
            nonce=nonce,
            encoded_identity_envelope=encoded,
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise S1RealmRuntimeControlError('二区 Runtime 控制授权失败')


def forwarded_request_headers(
    headers: object | None,
) -> dict[str, str]:
    """Project only explicitly approved client headers; never trust proxy ones."""
    forwarded = {'Accept-Encoding': 'identity'}
    if headers is None:
        return forwarded
    items = getattr(headers, 'items', None)
    if not callable(items):
        raise S1RealmRuntimeTargetError('二区 Runtime 请求头类型无效')
    try:
        header_items = tuple(items())
    except Exception as exc:
        raise S1RealmRuntimeTargetError(
            '二区 Runtime 请求头类型无效'
        ) from exc
    for entry in header_items:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise S1RealmRuntimeTargetError('二区 Runtime 请求头类型无效')
        raw_name, raw_value = entry
        canonical_name = FORWARDED_REQUEST_HEADERS.get(
            str(raw_name).lower()
        )
        if canonical_name is None or raw_value is None:
            continue
        value = str(raw_value)
        if '\r' in value or '\n' in value:
            raise S1RealmRuntimeTargetError('二区 Runtime 请求头值无效')
        forwarded[canonical_name] = value
    return forwarded


def filtered_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Project only inert representation metadata onto the central response.

    Region-2 is an internal trust boundary, not an origin that may set central
    cookies, redirects, authentication challenges, CORS policy, or transport
    headers.  A positive allowlist also prevents new upstream headers from
    silently acquiring authority on the public host.
    """
    projected: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        canonical_name = FORWARDED_RESPONSE_HEADERS.get(
            str(raw_name).lower()
        )
        if canonical_name is None:
            continue
        value = str(raw_value)
        if '\r' in value or '\n' in value:
            continue
        projected[canonical_name] = value
    return projected


def normalize_public_runtime_response(
    response: S1RealmRuntimeResponse,
) -> S1RealmRuntimeResponse:
    """Prevent an isolated Runtime from controlling central auth semantics."""
    if not isinstance(response, S1RealmRuntimeResponse):
        raise S1RealmRuntimeUnavailableError('二区 Runtime 响应类型无效')
    if (
        isinstance(response.status_code, bool)
        or not isinstance(response.status_code, int)
        or not 100 <= response.status_code <= 599
        or not isinstance(response.body, bytes)
    ):
        raise S1RealmRuntimeUnavailableError('二区 Runtime 响应无效')
    if len(response.body) > MAX_RUNTIME_RESPONSE_BODY_BYTES:
        raise S1RealmRuntimeUnavailableError('二区 Runtime 响应超过安全上限')

    headers = filtered_response_headers(response.headers)
    headers.update(_CENTRAL_RUNTIME_RESPONSE_HEADERS)
    if (
        response.status_code < 200
        or 300 <= response.status_code <= 399
        or response.status_code in {401, 407}
        or response.status_code >= 500
    ):
        body = json.dumps(
            {
                'code': 503,
                'message': '二区 Runtime 暂不可用，请稍后重试',
                'data': {'reason': 's1_region_2_upstream_unavailable'},
            },
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        error_headers = dict(_CENTRAL_RUNTIME_RESPONSE_HEADERS)
        # Retain the allowlisted overload delay while replacing the untrusted body.
        if response.status_code == 503 and 'Retry-After' in headers:
            error_headers['Retry-After'] = headers['Retry-After']
        return S1RealmRuntimeResponse(
            body=body,
            status_code=503,
            headers=error_headers,
        )
    return S1RealmRuntimeResponse(
        body=response.body,
        status_code=response.status_code,
        headers=headers,
    )


def _read_bounded_response_body(
    response: requests.Response,
    *,
    max_body_bytes: int,
    error_type: type[S1RealmRuntimeError],
    error_message: str,
) -> bytes:
    """Read one streamed upstream body without trusting Content-Length."""
    chunks: list[bytes] = []
    total_bytes = 0
    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > max_body_bytes:
                raise error_type(error_message)
            chunks.append(bytes(chunk))
    except S1RealmRuntimeError:
        raise
    except (TypeError, ValueError, requests.RequestException) as exc:
        raise error_type(error_message) from exc
    return b''.join(chunks)


def validate_proxy_request(
    *,
    resolved_realm_code: str,
    method: str,
    path: str,
    query_string: bytes | str | None = b'',
    body: bytes | bytearray | memoryview | None = None,
    headers: Mapping[str, str] | None = None,
) -> S1ValidatedRuntimeRequest:
    """Validate and freeze a request without injecting a secret or doing I/O."""
    require_region_2(resolved_realm_code)
    return S1ValidatedRuntimeRequest(
        method=normalize_method(method),
        path=validate_runtime_path(path),
        query_string=normalize_query_string(query_string),
        body=normalize_body(body),
        headers=forwarded_request_headers(headers),
    )


def _app_runtime_url() -> str:
    return validate_runtime_url(current_app.config.get(RUNTIME_URL_CONFIG_KEY))


def _app_readiness_expectation() -> S1RuntimeReadinessExpectation:
    from plugins.adventurer_s1.services.s1_deployment_identity_service import (
        S1_DEPLOYMENT_SCHEMA_REVISION,
    )

    active_contract = None
    if current_app.config.get(RUNTIME_MANAGED_LOCALLY_CONFIG_KEY) is True:
        from services.s1_region2_runtime_supervisor_service import (
            S1Region2RuntimeSupervisorService,
        )

        active_contract = (
            S1Region2RuntimeSupervisorService.active_runtime_contract(
                current_app._get_current_object()
            )
        )

    def contract_value(field_name: str, config_key: str, default=None):
        if active_contract is not None:
            return active_contract[field_name]
        return current_app.config.get(config_key, default)

    expected_server_mode = current_app.config.get(
        RUNTIME_EXPECTED_SERVER_MODE_CONFIG_KEY
    )
    if current_app.config.get('S1_RUNTIME_PRODUCTION_MODE') is True:
        if expected_server_mode != 'gunicorn':
            raise S1RealmRuntimeConfigurationError(
                'Production region-2 Runtime must use gunicorn'
            )
        if current_app.config.get(
            'ADVENTURER_S1_REGION_2_SOCKET_RELAY_ALLOW_PROCESS_LOCAL_REPLAY_GUARD'
        ) is True:
            raise S1RealmRuntimeConfigurationError(
                'Production region-2 relay cannot use process-local '
                'replay protection'
            )

    return S1RuntimeReadinessExpectation(
        runtime_id=current_app.config.get(
            RUNTIME_EXPECTED_RUNTIME_ID_CONFIG_KEY
        ),
        deployment_id=current_app.config.get(
            RUNTIME_EXPECTED_DEPLOYMENT_ID_CONFIG_KEY
        ),
        schema_revision=S1_DEPLOYMENT_SCHEMA_REVISION,
        runtime_contract_version=contract_value(
            'runtime_contract_version',
            RUNTIME_EXPECTED_CONTRACT_VERSION_CONFIG_KEY,
            RUNTIME_CONTRACT_VERSION,
        ),
        source_commit=contract_value(
            'source_commit',
            RUNTIME_EXPECTED_SOURCE_COMMIT_CONFIG_KEY
        ),
        source_tree=contract_value(
            'source_tree',
            RUNTIME_EXPECTED_SOURCE_TREE_CONFIG_KEY
        ),
        tracked_content_sha256=contract_value(
            'tracked_content_sha256',
            RUNTIME_EXPECTED_TRACKED_CONTENT_DIGEST_CONFIG_KEY
        ),
        required_route_digest=contract_value(
            'required_route_digest',
            RUNTIME_EXPECTED_ROUTE_DIGEST_CONFIG_KEY
        ),
        migration_manifest_sha256=contract_value(
            'migration_manifest_sha256',
            RUNTIME_EXPECTED_MIGRATION_MANIFEST_DIGEST_CONFIG_KEY
        ),
        runtime_host_release_digest=contract_value(
            'runtime_host_release_digest',
            RUNTIME_EXPECTED_HOST_RELEASE_DIGEST_CONFIG_KEY
        ),
        server_mode=expected_server_mode,
        ai_budget_namespace=current_app.config.get(
            RUNTIME_EXPECTED_AI_BUDGET_NAMESPACE_CONFIG_KEY
        ),
        ai_rate_limit_namespace=current_app.config.get(
            RUNTIME_EXPECTED_AI_RATE_LIMIT_NAMESPACE_CONFIG_KEY
        ),
        central_ai_credential_fingerprints=(
            _central_ai_credential_fingerprints(current_app.config)
        ),
        require_ai_ready=bool(current_app.config.get(
            'ADVENTURER_S1_REGION_2_REQUIRE_AI_READY_FOR_ADMISSION',
            True,
        )),
    )


def _app_runtime_timeouts(
    *,
    path: str | None = None,
    health: bool = False,
) -> tuple[float, float]:
    if health:
        read_key = RUNTIME_HEALTH_READ_TIMEOUT_CONFIG_KEY
        read_default = 5
    elif path and path.startswith(f'{S1_RUNTIME_PATH_PREFIX}/admin/'):
        read_key = RUNTIME_ADMIN_READ_TIMEOUT_CONFIG_KEY
        read_default = 150
    elif path and _AMBIENT_NPC_INTERACTION_PATH_RE.fullmatch(path):
        read_key = RUNTIME_INTERACTION_READ_TIMEOUT_CONFIG_KEY
        read_default = 75
    else:
        read_key = RUNTIME_READ_TIMEOUT_CONFIG_KEY
        read_default = 30
    return (
        validate_timeout(current_app.config.get(
            RUNTIME_CONNECT_TIMEOUT_CONFIG_KEY,
            3,
        )),
        validate_timeout(current_app.config.get(read_key, read_default)),
    )


class S1RealmRuntimeProxyService:
    """Bounded central transport facade for the fixed region-2 Runtime."""

    @staticmethod
    def proxy_request(
        *,
        resolved_realm_code: str,
        identity: S1RuntimeIdentitySnapshot,
        method: str,
        path: str,
        query_string: bytes | str | None = b'',
        body: bytes | bytearray | memoryview | None = None,
        headers: Mapping[str, str] | None = None,
        access_mode: str = RUNTIME_ACCESS_MODE_MEMBER,
        control_purpose: str | None = None,
    ) -> S1RealmRuntimeResponse:
        """Forward one authenticated S1 request to the isolated Runtime."""
        validated = validate_proxy_request(
            resolved_realm_code=resolved_realm_code,
            method=method,
            path=path,
            query_string=query_string,
            body=body,
            headers=headers,
        )
        identity_envelope = build_identity_envelope(
            identity=identity,
            expectation=_app_readiness_expectation(),
            method=validated.method,
            path=validated.path,
            query_string=validated.query_string,
            body=validated.body,
            proxy_secret=current_app.config.get(
                RUNTIME_PROXY_SECRET_CONFIG_KEY
            ),
            access_mode=access_mode,
        )
        request_headers = dict(validated.headers)
        request_headers.update(identity_envelope.headers)
        if control_purpose is not None:
            request_headers.update(build_runtime_control_headers(
                purpose=control_purpose,
                identity_envelope=identity_envelope,
                proxy_secret=current_app.config.get(
                    RUNTIME_PROXY_SECRET_CONFIG_KEY
                ),
            ))
        request_headers[RUNTIME_REALM_HEADER] = REGION_2
        url = f'{_app_runtime_url()}{validated.path}'
        if validated.query_string:
            url = f'{url}?{validated.query_string}'
        session_pool = _app_runtime_http_session_pool()
        session = session_pool.acquire()
        response = None
        reusable_session = True
        try:
            try:
                response = session.request(
                    validated.method,
                    url,
                    data=validated.body,
                    headers=request_headers,
                    timeout=_app_runtime_timeouts(path=validated.path),
                    allow_redirects=False,
                    stream=True,
                    proxies=_DIRECT_RUNTIME_PROXIES,
                )
            except requests.RequestException as exc:
                reusable_session = False
                raise S1RealmRuntimeUnavailableError(
                    '二区 Runtime 玩法请求不可用'
                ) from exc
            body_bytes = _read_bounded_response_body(
                response,
                max_body_bytes=MAX_RUNTIME_RESPONSE_BODY_BYTES,
                error_type=S1RealmRuntimeUnavailableError,
                error_message='二区 Runtime 响应超过安全上限或读取失败',
            )
            try:
                status_code = int(response.status_code)
                response_headers = dict(response.headers)
            except (AttributeError, TypeError, ValueError) as exc:
                raise S1RealmRuntimeUnavailableError(
                    '二区 Runtime 响应无效'
                ) from exc
            return S1RealmRuntimeResponse(
                body=body_bytes,
                status_code=status_code,
                headers=response_headers,
            )
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                reusable_session = False
                raise
            finally:
                session_pool.release(session, reusable=reusable_session)

    @staticmethod
    def check_readiness(
        resolved_realm_code: str = REGION_2,
    ) -> S1RuntimeReadinessReceipt:
        """Require readiness for either a managed-local or external Runtime."""
        require_region_2(resolved_realm_code)
        expectation = _app_readiness_expectation()
        if current_app.config.get(RUNTIME_MANAGED_LOCALLY_CONFIG_KEY) is True:
            runtime_url = _app_runtime_url()
            parsed_runtime_url = urlsplit(runtime_url)
            if (
                parsed_runtime_url.scheme != 'http'
                or parsed_runtime_url.hostname not in {
                    '127.0.0.1',
                    '::1',
                    'localhost',
                }
            ):
                raise S1RealmRuntimeConfigurationError(
                    '自动托管二区 Runtime 必须使用本机 loopback 地址'
                )
            health = S1RealmRuntimeProxyService.check_health(
                resolved_realm_code
            )
            if health.runtime_id != expectation.runtime_id:
                raise S1RealmRuntimeHealthError(
                    '目标进程未通过二区 Runtime 身份校验'
                )
            expected_manager_boot_id = current_app.config.get(
                RUNTIME_EXPECTED_MANAGER_BOOT_ID_CONFIG_KEY
            )
            if (
                not isinstance(expected_manager_boot_id, str)
                or not expected_manager_boot_id
                or len(expected_manager_boot_id) > 128
            ):
                raise S1RealmRuntimeConfigurationError(
                    '自动托管二区 Runtime 缺少主站启动批次'
                )
            if health.manager_boot_id != expected_manager_boot_id:
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime 属于旧主站启动批次，等待自动接管',
                    http_status=409,
                )
            # The supervisor passes this same requirement into its managed
            # child; HTTP 200 means the child enforced the selected queue mode.
            return _build_readiness_receipt(
                expectation,
                queue_isolation_mode=(
                    'redis_namespace'
                    if current_app.config.get(
                        'ADVENTURER_S1_REGION_2_REQUIRE_DURABLE_ASYNC_QUEUE',
                        True,
                    )
                    else 'process_local'
                ),
                pressure=health.pressure,
            )
        bootstrap_secret = current_app.config.get(
            RUNTIME_BOOTSTRAP_SECRET_CONFIG_KEY
        )
        challenge = build_readiness_challenge(
            expectation=expectation,
            proxy_secret=current_app.config.get(
                RUNTIME_PROXY_SECRET_CONFIG_KEY
            ),
            bootstrap_secret=bootstrap_secret,
        )
        session_pool = _app_runtime_http_session_pool()
        session = session_pool.acquire()
        response = None
        reusable_session = True
        try:
            try:
                response = session.request(
                    'POST',
                    f'{_app_runtime_url()}{RUNTIME_READINESS_PATH}',
                    data=_canonical_json_bytes(challenge.body),
                    headers={
                        **challenge.headers,
                        'Accept': 'application/json',
                        'Accept-Encoding': 'identity',
                    },
                    timeout=_app_runtime_timeouts(health=True),
                    allow_redirects=False,
                    stream=True,
                    proxies=_DIRECT_RUNTIME_PROXIES,
                )
            except requests.RequestException as exc:
                reusable_session = False
                raise S1RealmRuntimeUnavailableError(
                    '二区 Runtime readiness 请求不可用'
                ) from exc
            if response.status_code != 200:
                raise S1RealmRuntimeReadinessError(
                    f'二区 Runtime readiness 失败（HTTP {response.status_code}）',
                    http_status=int(response.status_code),
                )
            raw_body = _read_bounded_response_body(
                response,
                max_body_bytes=MAX_RUNTIME_READINESS_BODY_BYTES,
                error_type=S1RealmRuntimeReadinessError,
                error_message='二区 Runtime readiness 响应超过安全上限或读取失败',
            )
            try:
                payload = json.loads(raw_body.decode('utf-8'))
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise S1RealmRuntimeReadinessError(
                    '二区 Runtime readiness 响应无效'
                ) from exc
            return validate_readiness_response(
                payload=payload,
                challenge=challenge,
                bootstrap_secret=bootstrap_secret,
            )
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                reusable_session = False
                raise
            finally:
                session_pool.release(session, reusable=reusable_session)

    @staticmethod
    def check_health(
        resolved_realm_code: str = REGION_2,
    ) -> S1RealmRuntimeHealth:
        """Fail closed unless the configured process proves ready region-2 state."""
        require_region_2(resolved_realm_code)
        url = f'{_app_runtime_url()}{RUNTIME_HEALTH_PATH}'
        session_pool = _app_runtime_http_session_pool()
        session = session_pool.acquire()
        response = None
        reusable_session = True
        try:
            try:
                response = session.request(
                    'GET',
                    url,
                    headers={
                        'Accept': 'application/json',
                        'Accept-Encoding': 'identity',
                    },
                    timeout=_app_runtime_timeouts(health=True),
                    allow_redirects=False,
                    stream=True,
                    proxies=_DIRECT_RUNTIME_PROXIES,
                )
            except requests.RequestException as exc:
                reusable_session = False
                raise S1RealmRuntimeUnavailableError(
                    '二区 Runtime 健康检查不可用'
                ) from exc
            if response.status_code != 200:
                raise S1RealmRuntimeHealthError(
                    f'二区 Runtime 健康检查失败（HTTP {response.status_code}）'
                )
            try:
                raw_body = _read_bounded_response_body(
                    response,
                    max_body_bytes=MAX_RUNTIME_HEALTH_BODY_BYTES,
                    error_type=S1RealmRuntimeHealthError,
                    error_message='二区 Runtime 健康检查响应超过安全上限',
                )
                payload = json.loads(raw_body.decode('utf-8'))
            except S1RealmRuntimeHealthError:
                raise
            except (
                TypeError,
                ValueError,
                UnicodeDecodeError,
                requests.RequestException,
            ) as exc:
                raise S1RealmRuntimeHealthError(
                    '二区 Runtime 健康检查响应无效'
                ) from exc
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                reusable_session = False
                raise
            finally:
                session_pool.release(session, reusable=reusable_session)
        data = payload.get('data') if isinstance(payload, dict) else None
        if (
            not isinstance(data, dict)
            or payload.get('code') != 200
            or data.get('status') != 'ready'
            or data.get('realm_code') != REGION_2
            or data.get('plugin') != 'adventurer_s1'
        ):
            raise S1RealmRuntimeHealthError(
                '目标进程未通过二区 Runtime 身份校验'
            )
        runtime_id = data.get('runtime_id')
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise S1RealmRuntimeHealthError(
                '二区 Runtime 未返回有效 runtime_id'
            )
        manager_boot_id = data.get('manager_boot_id')
        if manager_boot_id is not None and (
            not isinstance(manager_boot_id, str)
            or manager_boot_id != manager_boot_id.strip()
            or not manager_boot_id
            or len(manager_boot_id) > 128
            or any(ord(char) < 32 for char in manager_boot_id)
        ):
            raise S1RealmRuntimeHealthError(
                '二区 Runtime 未返回有效主站启动批次'
            )
        return S1RealmRuntimeHealth(
            runtime_id=runtime_id.strip(),
            plugin='adventurer_s1',
            status='ready',
            realm_code=REGION_2,
            manager_boot_id=manager_boot_id,
            pressure=_runtime_pressure_from_health_data(data),
        )
