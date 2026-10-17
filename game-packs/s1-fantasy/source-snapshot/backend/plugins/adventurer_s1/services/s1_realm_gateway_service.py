"""Central request-routing decisions for isolated S1 runtimes."""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app, has_request_context, request
from itsdangerous import BadData, URLSafeTimedSerializer

from db_repository.s1_realm_repository import S1RealmRepository
from plugins.adventurer_s1.services.s1_realm_service import (
    REALM_1,
    REALM_2,
    S1RealmAssignmentError,
    S1RealmService,
)


@dataclass(frozen=True)
class S1GatewayRealmDecision:
    realm_code: str
    is_locked: bool
    should_set_cookie: bool = False


class S1RealmGatewayService:
    """Resolve the living-role realm plus explicit player/admin view targets."""

    COOKIE_NAME = 's1_realm_assignment'
    COOKIE_SALT = 's1-realm-assignment-v1'
    COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60

    @staticmethod
    def _serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(
            # Production startup already requires a non-development JWT key;
            # SECRET_KEY historically has a development fallback and is not a
            # sufficiently strong authority for immutable realm routing.
            current_app.config['JWT_SECRET_KEY'],
            salt=S1RealmGatewayService.COOKIE_SALT,
        )

    @staticmethod
    def encode_assignment_cookie(user_id: int, realm_code: str) -> str:
        realm = S1RealmService._validate_realm_code(realm_code)
        return S1RealmGatewayService._serializer().dumps({
            'v': 1,
            'user_id': int(user_id),
            'realm_code': realm,
        })

    @staticmethod
    def decode_assignment_cookie(
        raw_cookie: str | None,
        *,
        user_id: int,
    ) -> str | None:
        if not raw_cookie:
            return None
        try:
            payload = S1RealmGatewayService._serializer().loads(
                raw_cookie,
                max_age=S1RealmGatewayService.COOKIE_MAX_AGE_SECONDS,
            )
            if payload.get('v') != 1 or int(payload.get('user_id')) != int(user_id):
                return None
            return S1RealmService._validate_realm_code(payload.get('realm_code'))
        except (BadData, TypeError, ValueError, S1RealmAssignmentError):
            return None

    @staticmethod
    def resolve_player_realm(
        user_id: int,
        *,
        signed_cookie: str | None = None,
    ) -> S1GatewayRealmDecision:
        # A living slot is releasable on death/archive, so a long-lived signed
        # cookie can no longer be a routing authority.  It is decoded only
        # after the DB read to decide whether the compatibility hint is stale.
        uid = int(user_id)
        if has_request_context():
            request.s1_verified_assignment_preview = None
        membership_realm = S1RealmRepository.get_membership_realm_code(uid)
        preview = S1RealmService.get_assignment_preview(
            uid,
            membership_realm=membership_realm,
        )
        realm_code = S1RealmService._validate_realm_code(preview['realm_code'])
        is_locked = bool(preview.get('is_locked'))
        cookie_realm = S1RealmGatewayService.decode_assignment_cookie(
            signed_cookie,
            user_id=uid,
        )
        if has_request_context():
            # 只给同一请求的展示读取复用；路由授权每次仍读取当前 membership。
            request.s1_verified_assignment_preview = (uid, dict(preview))
        return S1GatewayRealmDecision(
            realm_code,
            is_locked,
            # Kept as a UI hint and for rolling-deploy compatibility only;
            # resolve_player_realm deliberately never trusts it as authority.
            is_locked and cookie_realm != realm_code,
        )

    @staticmethod
    def get_request_assignment_preview(user_id: int) -> dict | None:
        """Return this user's DB-verified projection from the current request."""
        if not has_request_context():
            return None
        verified = getattr(request, 's1_verified_assignment_preview', None)
        if verified is None or verified[0] != int(user_id):
            return None
        return dict(verified[1])

    @staticmethod
    def resolve_admin_target(raw_realm: str | None) -> str:
        if raw_realm in (None, REALM_1):
            return REALM_1
        if raw_realm == REALM_2:
            return REALM_2
        raise S1RealmAssignmentError('管理员区服目标无效')

    @staticmethod
    def resolve_player_view_target(raw_realm: str | None) -> str:
        """Validate a display target without treating it as membership."""
        if raw_realm == REALM_1:
            return REALM_1
        if raw_realm == REALM_2:
            return REALM_2
        raise S1RealmAssignmentError('玩家浏览区服目标无效')

    @staticmethod
    def attach_assignment_cookie(response, *, user_id: int, realm_code: str):
        response.set_cookie(
            S1RealmGatewayService.COOKIE_NAME,
            S1RealmGatewayService.encode_assignment_cookie(user_id, realm_code),
            max_age=S1RealmGatewayService.COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            secure=bool(current_app.config.get('SESSION_COOKIE_SECURE', True)),
            samesite='Lax',
            path='/api/adventurer-s1',
        )
        return response
