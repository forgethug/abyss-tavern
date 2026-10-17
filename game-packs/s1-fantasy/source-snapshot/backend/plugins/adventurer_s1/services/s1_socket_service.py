"""Deployment-owned, realm-scoped Socket.IO helpers for S1 runtimes."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any

from flask import current_app

from extensions import socketio
from db_repository.s1_realm_repository import S1RealmRepository
from plugins.adventurer_s1.services.s1_realm_service import (
    REALM_1,
    VALID_REALMS,
    S1RealmAssignmentError,
    S1RealmService,
)

S1_REALM_ROOM_PREFIX = 's1:realm:'
S1_ADMIN_REALM_ROOM_PREFIX = 's1:admin:realm:'
S1_ADMIN_REALM_EVENT = 's1_admin_realm_event'
S1_ADMIN_REALM_REVOKED_EVENT = 's1_admin_realm_subscription_revoked'
S1_PRIVATE_PLAYER_EVENT = '__s1_private_player_event__'
S1_ADMIN_SOCKET_LEASE_RECHECK_SECONDS = 5.0
# Contract shared with the isolated Runtime relay.  This is intentionally a
# declarative/static-audit list: the central emitter remains backward
# compatible and does not drop an event merely because the list is stale.
S1_SOCKET_EVENT_ALLOWLIST = frozenset({
    S1_PRIVATE_PLAYER_EVENT,
    'adventurer_changed',
    'adventurer_chat_message',
    'adventurer_s1_market_changed',
    'adventurer_s1_secret_auction_changed',
    'data_appeal_changed',
    'expedition_log_changed',
    'party_changed',
    'task_board_changed',
})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _S1AdminSocketLease:
    user_id: int
    realm_code: str
    room: str
    expires_at: int
    secondary_auth_epoch: int
    shared_lease_id: str
    generation: int
    revoked_at: float | None = None
    revoke_reason: str | None = None


@dataclass
class _S1AdminSocketUserCheck:
    lock: Any = field(default_factory=Lock)
    # Only scalar authorization results; never retain a session-bound User.
    result: tuple[float, str | None] | None = None


_admin_socket_lease_lock = Lock()
_admin_socket_lease_generation = 0
_admin_socket_leases: dict[str, _S1AdminSocketLease] = {}
_admin_socket_user_checks: dict[int, _S1AdminSocketUserCheck] = {}


class S1AdminSocketAuthorizationError(Exception):
    """Public, fail-closed reason for an admin realm subscription denial."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def s1_realm_room(realm_code: str) -> str:
    """Return the server-owned Socket.IO room for a validated S1 realm."""
    normalized = str(realm_code or '').strip()
    if normalized not in VALID_REALMS:
        raise S1RealmAssignmentError('S1 区服归属数据无效，请联系管理员')
    return f'{S1_REALM_ROOM_PREFIX}{normalized}'


def s1_admin_realm_room(realm_code: str) -> str:
    """Return the distinct admin observer room for one validated realm."""
    normalized = str(realm_code or '').strip()
    if normalized not in VALID_REALMS:
        raise S1RealmAssignmentError('S1 区服归属数据无效，请联系管理员')
    return f'{S1_ADMIN_REALM_ROOM_PREFIX}{normalized}'


def locked_s1_realm_room_for_user(user_id: int) -> str | None:
    """Resolve one persisted membership row; never infer or assign on connect."""
    membership = S1RealmRepository.get_membership(int(user_id))
    if membership is None:
        return None
    return s1_realm_room(membership.realm_code)


def deployment_s1_realm() -> str:
    """Return the immutable runtime realm, defaulting the central host to R1."""
    return S1RealmService.fixed_runtime_realm() or REALM_1


def _adventurer_refresh_hint(payload: dict) -> dict:
    """Only fields consumed by public card/building/party refresh handlers."""
    out = {key: payload[key] for key in (
        'action', 'reason', 'building_instance_id', 'new_dorm_room_id',
        'dorm_upgrade_room_id',
    ) if key in payload and isinstance(payload[key], (str, int, type(None)))}
    if isinstance(payload.get('ids'), list):
        out['ids'] = [value for value in payload['ids'] if type(value) is int and value > 0]
    return out


def emit_s1_to_realm(
    realm_code: str,
    event: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Emit one trusted S1 event to players and authorized realm observers.

    Player sockets retain the original event contract in their immutable
    membership room. Administrators receive a realm-tagged envelope in a
    separate observer room, so selecting an admin target never mutates or
    replaces the player's membership room.
    """
    if event == S1_PRIVATE_PLAYER_EVENT:
        envelope = args[0] if len(args) == 1 else None
        if not isinstance(envelope, dict) or set(envelope) != {'user_id', 'event', 'data'}:
            return False
        return emit_s1_to_user(
            realm_code, envelope['user_id'], envelope['event'], envelope['data'],
        )
    fixed_realm = S1RealmService.fixed_runtime_realm()
    if fixed_realm == 'region_2':
        if realm_code != fixed_realm:
            logger.warning(
                'Isolated S1 Runtime rejected cross-realm emit: realm=%s event=%s',
                realm_code,
                event,
            )
            return False
        relay = current_app.extensions.get('s1_region2_runtime_relay')
        if relay is None or len(args) > 1:
            logger.warning(
                'Isolated S1 Runtime dropped unsupported emit: event=%s args=%s',
                event,
                len(args),
            )
            return False
        # The central relay owns player/admin rooms.  The child supplies only
        # one JSON payload and can never select a central Socket.IO target.
        data = args[0] if args else None
        return relay.enqueue(event, data)

    kwargs.pop('to', None)
    kwargs.pop('room', None)
    from services.board_access_service import BoardAccessService
    kwargs['to'] = BoardAccessService.socket_room('adventurer_s1', s1_realm_room(realm_code))
    private_only = event == 'data_appeal_changed'
    naming_payloads = []
    if event == 'adventurer_changed':
        if len(args) != 1 or not isinstance(args[0], dict):
            return False
        payload = args[0]
        public = _adventurer_refresh_hint(payload)
        private_only = payload.get('action') == 'treasure_map_found'
        if payload.get('action') == 'building_completed':
            for key in ('dorm_naming_user_id', 'dorm_upgrade_naming_user_id'):
                if type(payload.get(key)) is int and payload[key] > 0:
                    naming_payloads.append((payload[key], {**public, key: payload[key]}))
        args = (public,)
    # Legacy runtimes cannot broadcast private results through a public event.
    result = None if private_only else socketio.emit(event, *args, **kwargs)
    for user_id, personal in naming_payloads:
        emit_s1_to_user(realm_code, user_id, event, personal)
    admin_kwargs: dict[str, Any] = {
        'to': s1_admin_realm_room(realm_code),
    }
    if kwargs.get('namespace') is not None:
        admin_kwargs['namespace'] = kwargs['namespace']
    try:
        socketio.emit(
            S1_ADMIN_REALM_EVENT,
            {
                'realm_code': realm_code,
                'event': event,
                'arguments': list(args),
            },
            **admin_kwargs,
        )
    except Exception:
        # Observer delivery is a best-effort refresh signal and must never
        # convert an already-completed gameplay write into a reported failure.
        logger.warning(
            'S1 admin realm Socket emit failed: realm=%s event=%s',
            realm_code,
            event,
            exc_info=True,
        )
    return result


def emit_s1_to_user(realm_code: str, user_id: int, event: str, data: dict) -> Any:
    """Deliver private S1 data to the authenticated user's room, including R2 relay.

    The signed child runtime chooses a user identity, never a Socket.IO room.
    The central host independently checks the user's permanent realm membership.
    """
    if type(user_id) is not int or user_id <= 0:
        return False
    if (
        not isinstance(event, str)
        or event not in {'adventurer_changed', 'data_appeal_changed'}
        or not isinstance(data, dict)
    ):
        return False
    fixed_realm = S1RealmService.fixed_runtime_realm()
    if fixed_realm == 'region_2':
        if realm_code != fixed_realm:
            return False
        relay = current_app.extensions.get('s1_region2_runtime_relay')
        return bool(relay and relay.enqueue(S1_PRIVATE_PLAYER_EVENT, {
            'user_id': user_id, 'event': event, 'data': data,
        }))

    delivered = False
    if locked_s1_realm_room_for_user(user_id) == s1_realm_room(realm_code):
        # The host joins this room only from the verified login identity.
        from services.board_access_service import BoardAccessService
        delivered = socketio.emit(event, data, to=BoardAccessService.socket_room(
            'adventurer_s1', str(user_id),
        ), namespace='/')
    if event == 'data_appeal_changed':
        socketio.emit(S1_ADMIN_REALM_EVENT, {
            'realm_code': realm_code, 'event': event, 'arguments': [data],
        }, to=s1_admin_realm_room(realm_code), namespace='/')
    return delivered


def emit_s1_building_result(adventurer_id: int, payload: dict) -> None:
    """Broadcast a refresh hint; send the result only to the persisted owner."""
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.s1_player_projection_service import (
        project_building_result,
    )

    public = _adventurer_refresh_hint(payload)
    realm = deployment_s1_realm()
    emit_s1_to_realm(realm, 'adventurer_changed', public, namespace='/')
    if not isinstance(payload.get('data'), dict):
        return
    owner_id = AdventurerS1.query.with_entities(AdventurerS1.owner_id).filter_by(
        id=adventurer_id,
    ).scalar()
    if owner_id:
        emit_s1_to_user(realm, int(owner_id), 'adventurer_changed', {
            **public, 'data': project_building_result(payload['data']),
        })


def _require_authenticated_socket_identity(user_id: int, sid: str) -> None:
    from utils.online_user_manager import online_user_manager

    normalized_sid = str(sid or '').strip()
    if not normalized_sid:
        raise S1AdminSocketAuthorizationError(
            'Socket 身份无效，请重新连接',
            'socket_identity_invalid',
        )
    try:
        mapped_user_id = online_user_manager.get_user_id_for_sid(normalized_sid)
    except Exception as exc:
        raise S1AdminSocketAuthorizationError(
            'Socket 身份暂时无法确认，请稍后重试',
            'socket_identity_unavailable',
        ) from exc
    if mapped_user_id is None or str(mapped_user_id) != str(int(user_id)):
        raise S1AdminSocketAuthorizationError(
            'Socket 身份不匹配，请重新连接',
            'socket_identity_mismatch',
        )


def _set_admin_s1_socket_lease(
    *,
    sid: str,
    user_id: int,
    realm_code: str,
    room: str,
    expires_at: int,
    secondary_auth_epoch: int,
    shared_lease_id: str,
) -> _S1AdminSocketLease:
    global _admin_socket_lease_generation
    with _admin_socket_lease_lock:
        previous = _admin_socket_leases.get(str(sid))
        _admin_socket_lease_generation += 1
        lease = _S1AdminSocketLease(
            user_id=int(user_id),
            realm_code=realm_code,
            room=room,
            expires_at=int(expires_at),
            secondary_auth_epoch=int(secondary_auth_epoch),
            shared_lease_id=str(shared_lease_id),
            generation=_admin_socket_lease_generation,
        )
        _admin_socket_leases[str(sid)] = lease
        # Admission already performs a fresh DB check. Do not reuse an earlier
        # negative result after the account has been re-enabled and admitted.
        _admin_socket_user_checks.pop(lease.user_id, None)
        if previous is not None:
            _discard_unused_admin_s1_user_check_locked(previous.user_id)
        return lease


def _current_admin_s1_socket_lease(sid: str) -> _S1AdminSocketLease | None:
    with _admin_socket_lease_lock:
        return _admin_socket_leases.get(str(sid))


def _discard_unused_admin_s1_user_check_locked(user_id: int) -> None:
    if not any(lease.user_id == user_id for lease in _admin_socket_leases.values()):
        _admin_socket_user_checks.pop(user_id, None)


def forget_admin_s1_realm_subscription(sid: str) -> None:
    """Confirm a disconnected local SID and release its shared lease marker."""
    normalized_sid = str(sid)
    lease = _current_admin_s1_socket_lease(normalized_sid)
    if lease is None:
        return
    try:
        from services.admin_secondary_auth_service import (
            AdminSecondaryAuthService,
        )

        AdminSecondaryAuthService.release_socket_lease(
            sid=normalized_sid,
            lease_id=lease.shared_lease_id,
        )
    except Exception:
        logger.warning(
            'S1 admin Socket disconnect marker release failed: sid=%s',
            normalized_sid,
            exc_info=True,
        )
        _mark_admin_s1_socket_lease_revoked(
            normalized_sid,
            reason='socket_disconnected',
            expected_generation=lease.generation,
        )
        return
    _remove_admin_s1_socket_lease(
        normalized_sid,
        expected_generation=lease.generation,
    )


def _remove_admin_s1_socket_lease(
    sid: str,
    *,
    expected_generation: int,
) -> bool:
    normalized_sid = str(sid)
    with _admin_socket_lease_lock:
        lease = _admin_socket_leases.get(normalized_sid)
        if lease is None or lease.generation != expected_generation:
            return False
        _admin_socket_leases.pop(normalized_sid, None)
        _discard_unused_admin_s1_user_check_locked(lease.user_id)
        return True


def _mark_admin_s1_socket_lease_revoked(
    sid: str,
    *,
    reason: str,
    expected_generation: int | None = None,
) -> _S1AdminSocketLease | None:
    normalized_sid = str(sid)
    with _admin_socket_lease_lock:
        lease = _admin_socket_leases.get(normalized_sid)
        if lease is None:
            return None
        if (
            expected_generation is not None
            and lease.generation != expected_generation
        ):
            return None
        if lease.revoked_at is not None:
            normalized_reason = str(reason or 'admin_access_revoked')
            if lease.revoke_reason != normalized_reason:
                lease = replace(lease, revoke_reason=normalized_reason)
                _admin_socket_leases[normalized_sid] = lease
            return lease
        revoked = replace(
            lease,
            revoked_at=time.time(),
            revoke_reason=str(reason or 'admin_access_revoked'),
        )
        _admin_socket_leases[normalized_sid] = revoked
        return revoked


def _create_revoked_admin_s1_socket_tombstone(
    *,
    sid: str,
    user_id: int,
    reason: str,
    secondary_auth_epoch: int,
    shared_lease_id: str,
) -> _S1AdminSocketLease:
    """Track a remote SID locally until cross-worker cleanup succeeds."""
    global _admin_socket_lease_generation
    normalized_sid = str(sid)
    with _admin_socket_lease_lock:
        existing = _admin_socket_leases.get(normalized_sid)
        if existing is not None and existing.user_id == int(user_id):
            return existing
        _admin_socket_lease_generation += 1
        lease = _S1AdminSocketLease(
            user_id=int(user_id),
            realm_code='',
            room='',
            expires_at=int(time.time()),
            secondary_auth_epoch=int(secondary_auth_epoch),
            shared_lease_id=str(shared_lease_id),
            generation=_admin_socket_lease_generation,
            revoked_at=time.time(),
            revoke_reason=str(reason or 'admin_access_revoked'),
        )
        _admin_socket_leases[normalized_sid] = lease
        if existing is not None:
            _discard_unused_admin_s1_user_check_locked(existing.user_id)
        return lease


def _attempt_revoked_admin_s1_socket_cleanup(
    sid: str,
    lease: _S1AdminSocketLease,
) -> bool:
    """Notify and isolate a revoked SID with a shared-marker acknowledgement."""
    normalized_sid = str(sid)
    current_lease = _current_admin_s1_socket_lease(normalized_sid)
    if (
        current_lease is None
        or current_lease.generation != lease.generation
        or current_lease.revoked_at is None
    ):
        # The tracked connection generation has already been cleared or
        # replaced. Never apply a delayed cleanup task to a newer SID lease.
        return True
    lease = current_lease
    reason = lease.revoke_reason or 'admin_access_revoked'

    from services.admin_secondary_auth_service import (
        AdminSecondaryAuthService,
        AdminSecondaryAuthStateUnavailable,
    )

    shared_lease_id: str | None = None
    shared_state_available = False
    try:
        shared_lease_id = AdminSecondaryAuthService.current_socket_lease_id(
            normalized_sid
        )
        shared_state_available = True
    except AdminSecondaryAuthStateUnavailable:
        logger.warning(
            'S1 admin Socket shared marker lookup failed: sid=%s reason=%s',
            normalized_sid,
            reason,
            exc_info=True,
        )

    is_remote_tombstone = not lease.room
    if is_remote_tombstone and shared_state_available:
        if shared_lease_id != lease.shared_lease_id:
            # The owning worker compare-deleted the exact old marker, or a new
            # subscription replaced it. This is the only cross-worker ACK;
            # successful Redis publish/leave/disconnect calls are not ACKs.
            _remove_admin_s1_socket_lease(
                normalized_sid,
                expected_generation=lease.generation,
            )
            return True
    elif (
        not is_remote_tombstone
        and shared_state_available
        and shared_lease_id is not None
        and shared_lease_id != lease.shared_lease_id
    ):
        # A newer lease owns this SID marker. A delayed old guard must never
        # leave or disconnect the replacement subscription.
        _remove_admin_s1_socket_lease(
            normalized_sid,
            expected_generation=lease.generation,
        )
        return True

    notified = False
    try:
        payload: dict[str, Any] = {'reason': reason}
        if lease.realm_code in VALID_REALMS:
            payload['realm_code'] = lease.realm_code
        socketio.emit(
            S1_ADMIN_REALM_REVOKED_EVENT,
            payload,
            to=normalized_sid,
            namespace='/',
        )
        notified = True
    except Exception:
        logger.warning(
            'S1 admin Socket revoke notification failed: sid=%s reason=%s',
            normalized_sid,
            reason,
            exc_info=True,
        )

    rooms_left = True
    for realm_code in sorted(VALID_REALMS):
        try:
            socketio.server.leave_room(
                normalized_sid,
                s1_admin_realm_room(realm_code),
                namespace='/',
            )
        except Exception:
            rooms_left = False
            logger.warning(
                'S1 admin Socket revoked room cleanup failed: '
                'sid=%s realm=%s reason=%s',
                normalized_sid,
                realm_code,
                reason,
                exc_info=True,
            )

    disconnect_requested = False
    if not rooms_left or not notified:
        try:
            # This is an acceleration request only. With a Redis Socket.IO
            # manager, a no-exception publish does not acknowledge execution
            # on the owning worker.
            socketio.server.disconnect(normalized_sid, namespace='/')
            disconnect_requested = True
        except Exception:
            logger.error(
                'S1 admin Socket revoke disconnect fallback failed: '
                'sid=%s reason=%s',
                normalized_sid,
                reason,
                exc_info=True,
            )

    if is_remote_tombstone:
        # The owning worker must remove the exact shared marker. Until then the
        # tombstone and retry loop remain, regardless of publish success.
        return False

    cleanup_complete = rooms_left and notified
    if cleanup_complete:
        try:
            AdminSecondaryAuthService.release_socket_lease(
                sid=normalized_sid,
                lease_id=lease.shared_lease_id,
            )
        except AdminSecondaryAuthStateUnavailable:
            logger.warning(
                'S1 admin Socket shared marker release failed: '
                'sid=%s reason=%s',
                normalized_sid,
                reason,
                exc_info=True,
            )
            return False
        _remove_admin_s1_socket_lease(
            normalized_sid,
            expected_generation=lease.generation,
        )
        return True

    if disconnect_requested:
        latest = _current_admin_s1_socket_lease(normalized_sid)
        if latest is None or latest.generation != lease.generation:
            return True
    return False


def _revoke_admin_s1_socket_lease(
    sid: str,
    *,
    reason: str,
    expected_generation: int | None = None,
) -> bool:
    lease = _mark_admin_s1_socket_lease_revoked(
        sid,
        reason=reason,
        expected_generation=expected_generation,
    )
    if lease is None:
        return False
    return _attempt_revoked_admin_s1_socket_cleanup(sid, lease)


def _admin_s1_socket_lease_failure_reason(
    lease: _S1AdminSocketLease,
    *,
    now: float | None = None,
    periodic_user_check: bool = False,
) -> str | None:
    if lease.revoked_at is not None:
        return lease.revoke_reason or 'admin_access_revoked'
    if lease.expires_at <= (time.time() if now is None else now):
        return 'secondary_auth_expired'
    from services.admin_secondary_auth_service import (
        AdminSecondaryAuthService,
        AdminSecondaryAuthStateUnavailable,
    )

    try:
        if not AdminSecondaryAuthService.epoch_is_current(
            lease.user_id,
            lease.secondary_auth_epoch,
        ):
            return 'secondary_auth_epoch_revoked'
    except AdminSecondaryAuthStateUnavailable:
        return 'secondary_auth_state_unavailable'
    if periodic_user_check:
        return _periodic_admin_s1_socket_user_failure_reason(lease.user_id)
    return _admin_s1_socket_user_failure_reason(lease.user_id)


def _admin_s1_socket_user_failure_reason(user_id: int) -> str | None:
    from db_repository import db_manager

    try:
        user = db_manager.user.get_by_id_for_auth(user_id, independent_read=True)
    except Exception:
        logger.warning(
            'S1 admin Socket lease user refresh failed closed: user_id=%s',
            user_id,
            exc_info=True,
        )
        return 'admin_access_unavailable'
    if (
        user is None
        or not user.is_admin
        or user.is_deleted
        or user.is_frozen
    ):
        return 'admin_access_revoked'
    return None


def _periodic_admin_s1_socket_user_failure_reason(user_id: int) -> str | None:
    """Coalesce same-account guards within their existing recheck interval."""
    with _admin_socket_lease_lock:
        # The SID may have disconnected while its epoch check was in flight.
        # Do not create an orphan cache entry after the last lease is removed.
        if any(lease.user_id == user_id for lease in _admin_socket_leases.values()):
            check = _admin_socket_user_checks.setdefault(user_id, _S1AdminSocketUserCheck())
        else:
            check = None
    if check is None:
        return _admin_s1_socket_user_failure_reason(user_id)
    with check.lock:
        checked_at = time.monotonic()
        if check.result is not None and checked_at < check.result[0]:
            return check.result[1]
        reason = _admin_s1_socket_user_failure_reason(user_id)
        check.result = (checked_at + S1_ADMIN_SOCKET_LEASE_RECHECK_SECONDS, reason)
        return reason


def _admin_s1_socket_guard_delay(lease: _S1AdminSocketLease) -> float:
    delay = min(S1_ADMIN_SOCKET_LEASE_RECHECK_SECONDS, lease.expires_at - time.time())
    with _admin_socket_lease_lock:
        check = _admin_socket_user_checks.get(lease.user_id)
        result = check.result if check is not None else None
    if result is not None:
        # Align the next wake-up to the shared read's deadline. A cache hit
        # must never add another five seconds before a downgrade is checked.
        delay = min(delay, result[0] - time.monotonic())
    return max(0.0, delay)


def enforce_admin_s1_socket_lease(
    sid: str,
    *,
    expected_generation: int | None = None,
    now: float | None = None,
    periodic_user_check: bool = False,
) -> bool:
    """Recheck one lease and revoke it on expiry or administrator downgrade."""
    lease = _current_admin_s1_socket_lease(sid)
    if lease is None:
        return False
    if expected_generation is not None and lease.generation != expected_generation:
        return False
    reason = _admin_s1_socket_lease_failure_reason(
        lease, now=now, periodic_user_check=periodic_user_check,
    )
    if reason is None:
        return True
    _revoke_admin_s1_socket_lease(
        sid,
        reason=reason,
        expected_generation=lease.generation,
    )
    return False


def _run_admin_s1_socket_lease_guard(app, sid: str, generation: int) -> None:
    """Server-owned lease guard; client refresh is never the authority."""
    with app.app_context():
        while True:
            lease = _current_admin_s1_socket_lease(sid)
            if lease is None or lease.generation != generation:
                return
            if lease.revoked_at is not None:
                socketio.sleep(S1_ADMIN_SOCKET_LEASE_RECHECK_SECONDS)
                lease = _current_admin_s1_socket_lease(sid)
                if lease is None or lease.generation != generation:
                    return
                try:
                    if _attempt_revoked_admin_s1_socket_cleanup(sid, lease):
                        return
                finally:
                    from extensions import db

                    db.session.remove()
                continue
            delay = _admin_s1_socket_guard_delay(lease)
            if delay > 0:
                socketio.sleep(delay)
            try:
                if not enforce_admin_s1_socket_lease(
                    sid,
                    expected_generation=generation,
                    periodic_user_check=True,
                ):
                    if _current_admin_s1_socket_lease(sid) is None:
                        return
            finally:
                from extensions import db

                db.session.remove()


def _start_admin_s1_socket_lease_guard(
    lease: _S1AdminSocketLease,
    sid: str,
) -> None:
    app = current_app._get_current_object()
    socketio.start_background_task(
        _run_admin_s1_socket_lease_guard,
        app,
        str(sid),
        lease.generation,
    )


def _accelerate_untracked_admin_s1_socket_revocation(
    sid: str,
    *,
    reason: str,
) -> None:
    """Best-effort UI/room cleanup for an online SID with no shared lease."""
    normalized_sid = str(sid)
    try:
        socketio.emit(
            S1_ADMIN_REALM_REVOKED_EVENT,
            {'reason': reason},
            to=normalized_sid,
            namespace='/',
        )
    except Exception:
        logger.warning(
            'S1 admin Socket untracked revoke notification failed: sid=%s',
            normalized_sid,
            exc_info=True,
        )
    for realm_code in sorted(VALID_REALMS):
        try:
            socketio.server.leave_room(
                normalized_sid,
                s1_admin_realm_room(realm_code),
                namespace='/',
            )
        except Exception:
            logger.warning(
                'S1 admin Socket untracked room cleanup failed: '
                'sid=%s realm=%s',
                normalized_sid,
                realm_code,
                exc_info=True,
            )


def revoke_admin_s1_realm_subscriptions_for_user(
    user_id: int,
    *,
    reason: str = 'secondary_auth_locked',
) -> int:
    """Immediately revoke every known local or cross-worker admin SID."""
    uid = int(user_id)
    from services.admin_secondary_auth_service import (
        AdminSecondaryAuthService,
        AdminSecondaryAuthStateUnavailable,
    )

    try:
        current_auth_epoch = AdminSecondaryAuthService.current_epoch(uid)
    except AdminSecondaryAuthStateUnavailable:
        current_auth_epoch = 0
    with _admin_socket_lease_lock:
        local_sids = {
            sid
            for sid, lease in _admin_socket_leases.items()
            if lease.user_id == uid
        }
    try:
        from utils.online_user_manager import online_user_manager

        online_sids = {
            str(sid)
            for sid in online_user_manager.get_user_sids(str(uid))
            if sid
        }
    except Exception:
        online_sids = set()
        logger.warning(
            'S1 admin Socket user SID lookup failed: user_id=%s',
            uid,
            exc_info=True,
        )

    targets = sorted(local_sids | online_sids)
    for sid in targets:
        lease = _current_admin_s1_socket_lease(sid)
        if lease is None or lease.user_id != uid:
            try:
                shared_lease_id = (
                    AdminSecondaryAuthService.current_socket_lease_id(sid)
                )
            except AdminSecondaryAuthStateUnavailable:
                shared_lease_id = None
                logger.warning(
                    'S1 admin Socket remote marker lookup failed: '
                    'user_id=%s sid=%s',
                    uid,
                    sid,
                    exc_info=True,
                )
            if shared_lease_id is None:
                _accelerate_untracked_admin_s1_socket_revocation(
                    sid,
                    reason=reason,
                )
                continue
            lease = _create_revoked_admin_s1_socket_tombstone(
                sid=sid,
                user_id=uid,
                reason=reason,
                secondary_auth_epoch=current_auth_epoch,
                shared_lease_id=shared_lease_id,
            )
            try:
                _start_admin_s1_socket_lease_guard(lease, sid)
            except Exception:
                logger.warning(
                    'S1 admin Socket remote tombstone guard start failed: '
                    'user_id=%s sid=%s',
                    uid,
                    sid,
                    exc_info=True,
                )
        _revoke_admin_s1_socket_lease(
            sid,
            reason=reason,
            expected_generation=lease.generation,
        )
    return len(targets)


def switch_admin_s1_realm_subscription(
    *,
    user_id: int,
    sid: str,
    realm_code: str,
    secondary_grant: str,
) -> str:
    """Authorize one SID and atomically switch its admin observer room."""
    if type(realm_code) is not str or realm_code not in VALID_REALMS:
        raise S1AdminSocketAuthorizationError(
            '管理区服参数无效',
            'invalid_realm_code',
        )
    uid = int(user_id)
    _require_authenticated_socket_identity(uid, sid)

    from db_repository import db_manager
    from services.admin_secondary_auth_service import (
        AdminSecondaryAuthService,
        AdminSecondaryAuthStateUnavailable,
    )

    user = db_manager.user.get_by_id_for_auth(uid, independent_read=True)
    if (
        user is None
        or bool(user.is_deleted)
        or bool(user.is_frozen)
        or not bool(user.is_admin)
    ):
        raise S1AdminSocketAuthorizationError(
            '仅管理员可订阅区服实时事件',
            'admin_required',
        )
    grant = AdminSecondaryAuthService.inspect_socket_read_grant(
        secondary_grant,
        uid,
    )
    if not AdminSecondaryAuthService.grant_allows(
        grant,
        AdminSecondaryAuthService.SCOPE_READ,
    ):
        raise S1AdminSocketAuthorizationError(
            '需要管理员只读二级授权',
            AdminSecondaryAuthService.REQUIRED_REASON,
        )

    expires_at = int(grant['expires_at'])
    secondary_auth_epoch = int(grant['auth_epoch'])

    normalized_sid = str(sid)
    existing_lease = _current_admin_s1_socket_lease(normalized_sid)
    if existing_lease is not None and existing_lease.revoked_at is not None:
        # Never let a reconnect/switch race re-authorize a SID while a failed
        # cross-worker room cleanup is still being retried.
        raise S1AdminSocketAuthorizationError(
            '上一次区服订阅仍在安全清理，请稍后重试',
            'subscription_cleanup_pending',
        )
    shared_lease_id = secrets.token_urlsafe(24)
    try:
        claimed = AdminSecondaryAuthService.claim_socket_lease(
            user_id=uid,
            sid=normalized_sid,
            auth_epoch=secondary_auth_epoch,
            lease_id=shared_lease_id,
            expires_at=expires_at,
        )
    except AdminSecondaryAuthStateUnavailable as exc:
        raise S1AdminSocketAuthorizationError(
            '二级授权共享状态暂时不可用，请稍后重试',
            'secondary_auth_state_unavailable',
        ) from exc
    if not claimed:
        raise S1AdminSocketAuthorizationError(
            '二级授权已失效，请重新验证',
            'secondary_auth_epoch_revoked',
        )
    target_room = s1_admin_realm_room(realm_code)
    try:
        # Remove only observer rooms. The immutable player membership room uses
        # a different prefix and is deliberately untouched.
        for candidate_realm in sorted(VALID_REALMS):
            candidate_room = s1_admin_realm_room(candidate_realm)
            if candidate_room != target_room:
                socketio.server.leave_room(
                    normalized_sid,
                    candidate_room,
                    namespace='/',
                )
        socketio.server.enter_room(normalized_sid, target_room, namespace='/')
        lease = _set_admin_s1_socket_lease(
            sid=normalized_sid,
            user_id=uid,
            realm_code=realm_code,
            room=target_room,
            expires_at=expires_at,
            secondary_auth_epoch=secondary_auth_epoch,
            shared_lease_id=shared_lease_id,
        )
        _start_admin_s1_socket_lease_guard(lease, normalized_sid)
        if not enforce_admin_s1_socket_lease(
            normalized_sid,
            expected_generation=lease.generation,
        ):
            raise S1AdminSocketAuthorizationError(
                '二级授权已失效，请重新验证',
                'secondary_auth_epoch_revoked',
            )
    except Exception:
        lease = _current_admin_s1_socket_lease(normalized_sid)
        if lease is not None:
            _revoke_admin_s1_socket_lease(
                normalized_sid,
                reason='subscription_guard_unavailable',
                expected_generation=lease.generation,
            )
        room_cleanup_failed = False
        for candidate_realm in sorted(VALID_REALMS):
            try:
                socketio.server.leave_room(
                    normalized_sid,
                    s1_admin_realm_room(candidate_realm),
                    namespace='/',
                )
            except Exception:
                room_cleanup_failed = True
        if room_cleanup_failed:
            try:
                socketio.server.disconnect(normalized_sid, namespace='/')
            except Exception:
                logger.error(
                    'S1 admin Socket failed subscription isolation failed: '
                    'user_id=%s sid=%s',
                    uid,
                    normalized_sid,
                    exc_info=True,
                )
        try:
            AdminSecondaryAuthService.release_socket_lease(
                sid=normalized_sid,
                lease_id=shared_lease_id,
            )
        except Exception:
            logger.warning(
                'S1 admin Socket failed subscription marker cleanup failed: '
                'user_id=%s sid=%s',
                uid,
                normalized_sid,
                exc_info=True,
            )
        raise
    return target_room


def clear_admin_s1_realm_subscription(*, user_id: int, sid: str) -> None:
    """Leave all admin observer rooms without touching player membership."""
    uid = int(user_id)
    _require_authenticated_socket_identity(uid, sid)
    normalized_sid = str(sid)
    lease = _current_admin_s1_socket_lease(normalized_sid)
    created_tombstone = lease is None or lease.user_id != uid
    if created_tombstone:
        from services.admin_secondary_auth_service import (
            AdminSecondaryAuthService,
            AdminSecondaryAuthStateUnavailable,
        )

        try:
            shared_lease_id = (
                AdminSecondaryAuthService.current_socket_lease_id(
                    normalized_sid
                )
            )
            secondary_auth_epoch = AdminSecondaryAuthService.current_epoch(uid)
        except AdminSecondaryAuthStateUnavailable:
            shared_lease_id = None
            secondary_auth_epoch = 0
        if shared_lease_id is None:
            _accelerate_untracked_admin_s1_socket_revocation(
                normalized_sid,
                reason='client_unsubscribed',
            )
            return
        lease = _create_revoked_admin_s1_socket_tombstone(
            sid=normalized_sid,
            user_id=uid,
            reason='client_unsubscribed',
            secondary_auth_epoch=secondary_auth_epoch,
            shared_lease_id=shared_lease_id,
        )
    assert lease is not None
    cleanup_complete = _revoke_admin_s1_socket_lease(
        normalized_sid,
        reason='client_unsubscribed',
        expected_generation=lease.generation,
    )
    if created_tombstone and not cleanup_complete:
        _start_admin_s1_socket_lease_guard(lease, normalized_sid)


def join_user_sockets_to_realm(user_id: int, realm_code: str) -> int:
    """Best-effort rejoin after a committed first membership assignment.

    The caller must pass server-owned scalar values only after the membership
    transaction commits. A short, independent board-permission read finishes
    before Redis-backed Socket.IO dispatches ``enter_room`` to the worker that owns a
    SID when the user's connections live in another process.
    """
    uid = int(user_id)
    if uid <= 0:
        return 0
    from services.board_access_service import BoardAccessService
    room = BoardAccessService.socket_room('adventurer_s1', s1_realm_room(realm_code), user_id=uid)
    if room is None:
        return 0
    try:
        from utils.online_user_manager import online_user_manager
        sids = online_user_manager.get_user_sids(str(uid))
    except Exception:
        logger.warning(
            'S1 Socket rejoin lookup failed: user_id=%s realm=%s',
            uid,
            realm_code,
            exc_info=True,
        )
        return 0

    joined = 0
    for sid in sorted({str(value) for value in sids if value}):
        try:
            socketio.server.enter_room(sid, room, namespace='/')
            joined += 1
        except Exception:
            logger.warning(
                'S1 Socket rejoin failed: user_id=%s sid=%s realm=%s',
                uid,
                sid,
                realm_code,
                exc_info=True,
            )
    return joined


class _S1DeploymentSocketEmitter:
    """Scope otherwise-global S1 emits to the current deployment's realm.

    Explicit ``to``/``room`` targets are preserved so existing personal and
    administrator-room notifications retain their original audience.
    """

    @staticmethod
    def emit(event: str, *args: Any, **kwargs: Any) -> Any:
        has_explicit_target = (
            kwargs.get('to') is not None or kwargs.get('room') is not None
        )
        if not has_explicit_target:
            return emit_s1_to_realm(
                deployment_s1_realm(),
                event,
                *args,
                **kwargs,
            )
        if S1RealmService.fixed_runtime_realm() != 'region_2':
            from services.board_access_service import BoardAccessService
            for key in ('to', 'room'):
                target = kwargs.get(key)
                if isinstance(target, (str, int)) and str(target).isdigit():
                    kwargs[key] = BoardAccessService.socket_room('adventurer_s1', str(target))
        return socketio.emit(event, *args, **kwargs)


s1_socketio = _S1DeploymentSocketEmitter()
