"""S1 realm browsing plus the cross-runtime single-living-adventurer slot."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from dataclasses import dataclass
from datetime import datetime, timedelta
import secrets
from typing import Any

from flask import current_app

from db_repository.s1_realm_repository import S1RealmRepository
from plugins.adventurer_s1.models import AdventurerS1RealmMembership
from plugins.adventurer_s1.services.adventurer_s1_settings_service import (
    S1SettingsService,
)

REALM_1 = 'region_1'
REALM_2 = 'region_2'
VALID_REALMS = frozenset((REALM_1, REALM_2))
REALM_LABELS = {REALM_1: '一区', REALM_2: '二区'}
SLOT_PENDING = 'pending'
SLOT_ACTIVE = 'active'
VALID_SLOT_STATES = frozenset((SLOT_PENDING, SLOT_ACTIVE))
_MEMBERSHIP_REALM_NOT_LOADED = object()


class S1RealmAssignmentError(RuntimeError):
    """Realm or global active-slot state is invalid."""


class S1RealmActiveSlotError(S1RealmAssignmentError):
    """The account already has a finalized living role."""


class S1RealmActiveSlotBusyError(S1RealmAssignmentError):
    """Another attempt holds a temporary claim that may still abort."""


class S1Region2AdmissionUnavailableError(S1RealmAssignmentError):
    """Region 2 is closed or its isolated Runtime is not ready."""


@dataclass(frozen=True, slots=True)
class S1Region2AdmissionSettingLease:
    """Detached revision of the persisted central region-2 admission switch."""

    setting_id: int
    updated_at: datetime


class S1RealmService:
    @staticmethod
    def fixed_runtime_realm() -> str | None:
        configured = current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE')
        if configured in (None, ''):
            return None
        return S1RealmService._validate_realm_code(str(configured))

    @staticmethod
    def current_runtime_realm() -> str:
        """Central gameplay owns region 1; the isolated Runtime owns region 2."""
        return S1RealmService.fixed_runtime_realm() or REALM_1

    @staticmethod
    def realm_label(realm_code: str) -> str:
        return REALM_LABELS.get(realm_code, '未知区服')

    @staticmethod
    def _validate_realm_code(realm_code: str) -> str:
        value = str(realm_code or '').strip()
        if value not in VALID_REALMS:
            raise S1RealmAssignmentError('S1 区服数据无效，请联系管理员')
        return value

    @staticmethod
    def _validate_slot_state(slot_state: str) -> str:
        value = str(slot_state or '').strip()
        if value not in VALID_SLOT_STATES:
            raise S1RealmAssignmentError('S1 全局角色槽位状态无效，请联系管理员')
        return value

    @staticmethod
    def _infer_history(user_id: int) -> tuple[str | None, str | None]:
        """Repair-only fallback from a currently living local role, never a queue."""
        evidence = S1RealmRepository.list_history_realm_evidence(user_id)
        if not evidence:
            return None, None
        realms = {S1RealmService._validate_realm_code(code) for _, code in evidence}
        if len(realms) != 1:
            raise S1RealmAssignmentError('S1 存活冒险者区服冲突，请联系管理员')
        return next(iter(realms)), 'living_adventurer_fallback'

    @staticmethod
    def get_assignment_preview(
        user_id: int | None,
        *,
        membership_realm: object = _MEMBERSHIP_REALM_NOT_LOADED,
    ) -> dict:
        """Project the active-role realm; reservations never lock browsing."""
        fixed_realm = S1RealmService.fixed_runtime_realm()
        if user_id is None:
            realm_code = fixed_realm or REALM_1
            return {
                'realm_code': realm_code,
                'realm_label': REALM_LABELS[realm_code],
                'is_locked': False,
            }
        uid = int(user_id)
        if membership_realm is _MEMBERSHIP_REALM_NOT_LOADED:
            membership_realm = S1RealmRepository.get_membership_realm_code(uid)
        if membership_realm is not None:
            realm_code = S1RealmService._validate_realm_code(str(membership_realm))
            if fixed_realm is not None and realm_code != fixed_realm:
                raise S1RealmAssignmentError('账号存活冒险者不属于当前 S1 Runtime')
            return {
                'realm_code': realm_code,
                'realm_label': REALM_LABELS[realm_code],
                'is_locked': True,
            }
        historical_realm, _ = S1RealmService._infer_history(uid)
        if historical_realm is not None:
            if fixed_realm is not None and historical_realm != fixed_realm:
                raise S1RealmAssignmentError('账号存活冒险者不属于当前 S1 Runtime')
            return {
                'realm_code': historical_realm,
                'realm_label': REALM_LABELS[historical_realm],
                'is_locked': True,
            }
        realm_code = fixed_realm or REALM_1
        return {
            'realm_code': realm_code,
            'realm_label': REALM_LABELS[realm_code],
            'is_locked': False,
        }

    @staticmethod
    def _region_2_setting_is_enabled(raw_value: object) -> bool:
        return str(raw_value or '').strip().lower() in {'1', 'true', 'yes', 'on'}

    @staticmethod
    def capture_region_2_admission_setting_lease(
    ) -> S1Region2AdmissionSettingLease:
        """Capture the exact enabled revision before external readiness I/O."""
        setting = S1RealmRepository.get_region_2_admission_setting()
        if (
            setting is None
            or setting.updated_at is None
            or not S1RealmService._region_2_setting_is_enabled(setting.value)
        ):
            raise S1Region2AdmissionUnavailableError('二区体验服当前未开放预约')
        return S1Region2AdmissionSettingLease(
            setting_id=int(setting.id),
            updated_at=setting.updated_at,
        )

    @staticmethod
    def require_region_2_admission_lease(
        *,
        setting_lease: S1Region2AdmissionSettingLease,
        readiness_receipt: object,
    ) -> None:
        """Revalidate the switch after Runtime readiness without spanning I/O."""
        locked_setting = S1RealmRepository.get_region_2_admission_setting(
            for_update=True,
        )
        if (
            locked_setting is None
            or not S1RealmService._region_2_setting_is_enabled(locked_setting.value)
            or not isinstance(setting_lease, S1Region2AdmissionSettingLease)
            or int(locked_setting.id) != setting_lease.setting_id
            or locked_setting.updated_at != setting_lease.updated_at
        ):
            raise S1Region2AdmissionUnavailableError(
                '二区准入设置已变化，请刷新后重试'
            )
        from plugins.adventurer_s1.services.s1_realm_runtime_proxy_service import (
            S1RuntimeReadinessReceipt,
        )

        if (
            not isinstance(readiness_receipt, S1RuntimeReadinessReceipt)
            or readiness_receipt.realm_code != REALM_2
            or readiness_receipt.status != 'ready'
        ):
            raise S1Region2AdmissionUnavailableError(
                '二区 Runtime 未提供本轮受信 readiness 回执'
            )

    @staticmethod
    def _pending_ttl() -> timedelta:
        raw = current_app.config.get(
            'ADVENTURER_S1_ACTIVE_SLOT_PENDING_TTL_SECONDS',
            600,
        )
        try:
            seconds = int(raw)
        except (TypeError, ValueError) as exc:
            raise S1RealmAssignmentError('S1 全局角色槽位超时配置无效') from exc
        if seconds < 30 or seconds > 3600:
            raise S1RealmAssignmentError('S1 全局角色槽位超时配置无效')
        return timedelta(seconds=seconds)

    @staticmethod
    def _active_absence_grace() -> timedelta:
        raw = current_app.config.get(
            'ADVENTURER_S1_ACTIVE_SLOT_ABSENCE_GRACE_SECONDS',
            180,
        )
        try:
            seconds = int(raw)
        except (TypeError, ValueError) as exc:
            raise S1RealmAssignmentError('S1 全局角色槽位缺席保护配置无效') from exc
        if seconds < 60 or seconds > 3600:
            raise S1RealmAssignmentError('S1 全局角色槽位缺席保护配置无效')
        return timedelta(seconds=seconds)

    @staticmethod
    def _pending_is_expired(
        membership: AdventurerS1RealmMembership,
        *,
        now: datetime,
    ) -> bool:
        if S1RealmService._validate_slot_state(membership.slot_state) != SLOT_PENDING:
            return False
        changed_at = membership.updated_at or membership.created_at
        return changed_at is None or changed_at + S1RealmService._pending_ttl() <= now

    @staticmethod
    def claim_active_slot(
        user_id: int,
        *,
        realm_code: str,
        reservation_id: int,
        claim_token: str | None = None,
        assignment_reason: str = 'reservation_draw_claim',
        now: datetime | None = None,
    ) -> AdventurerS1RealmMembership:
        """Claim the one central slot; caller owns the surrounding transaction."""
        uid = int(user_id)
        realm = S1RealmService._validate_realm_code(realm_code)
        rid = int(reservation_id)
        if rid <= 0:
            raise S1RealmAssignmentError('预约标识无效，无法认领全局角色槽位')
        requested_token = str(claim_token or '')
        if claim_token is not None and (
            len(requested_token) != 32
            or any(char not in '0123456789abcdef' for char in requested_token)
        ):
            raise S1RealmAssignmentError('全局角色槽位认领令牌无效')
        if S1RealmRepository.lock_user(uid) is None:
            raise S1RealmAssignmentError('用户不存在，无法认领全局角色槽位')
        current = S1RealmRepository.get_membership(uid, for_update=True)
        claimed_at = now or _portable_utc_now()
        if current is not None:
            state = S1RealmService._validate_slot_state(current.slot_state)
            if state == SLOT_PENDING:
                if (
                    current.realm_code == realm
                    and current.claim_reservation_id == rid
                    and (
                        claim_token is None
                        or secrets.compare_digest(
                            str(current.claim_token or ''),
                            requested_token,
                        )
                    )
                ):
                    return current
                raise S1RealmActiveSlotBusyError(
                    '该账号正在生成另一个冒险者，请稍后重试'
                )
            # A timed-out claim must stay fail-closed here.  The claiming
            # Runtime may already have committed the adventurer while its
            # finalize callback was interrupted.  Only that realm's complete
            # reconciliation inventory can prove that an old pending claim is
            # safe to release.
            label = S1RealmService.realm_label(current.realm_code)
            raise S1RealmActiveSlotError(
                f'该账号已由{label}占用唯一存活冒险者名额'
            )
        membership = AdventurerS1RealmMembership(
            user_id=uid,
            realm_code=realm,
            assignment_reason=str(assignment_reason)[:50],
            slot_state=SLOT_PENDING,
            claim_token=requested_token or secrets.token_hex(16),
            claim_reservation_id=rid,
            active_adventurer_id=None,
            created_at=claimed_at,
            updated_at=claimed_at,
        )
        S1RealmRepository.add_membership(membership)
        return membership

    @staticmethod
    def finalize_active_slot(
        user_id: int,
        *,
        realm_code: str,
        reservation_id: int,
        claim_token: str,
        adventurer_id: int,
        now: datetime | None = None,
    ) -> AdventurerS1RealmMembership:
        """Promote the exact pending claim after the adventurer is durable."""
        uid = int(user_id)
        realm = S1RealmService._validate_realm_code(realm_code)
        rid = int(reservation_id)
        aid = int(adventurer_id)
        if S1RealmRepository.lock_user(uid) is None:
            raise S1RealmAssignmentError('用户不存在，无法确认全局角色槽位')
        membership = S1RealmRepository.get_membership(uid, for_update=True)
        if membership is None:
            raise S1RealmAssignmentError('全局角色槽位认领不存在')
        state = S1RealmService._validate_slot_state(membership.slot_state)
        if (
            state == SLOT_ACTIVE
            and membership.realm_code == realm
            and membership.active_adventurer_id == aid
        ):
            if membership.reconcile_absent_since is not None:
                membership.reconcile_absent_since = None
                membership.updated_at = now or _portable_utc_now()
            return membership
        if (
            state != SLOT_PENDING
            or membership.realm_code != realm
            or membership.claim_reservation_id != rid
            or not secrets.compare_digest(
                str(membership.claim_token or ''),
                str(claim_token or ''),
            )
        ):
            raise S1RealmActiveSlotError('全局角色槽位认领已变化，拒绝生成第二个角色')
        membership.slot_state = SLOT_ACTIVE
        membership.assignment_reason = 'living_adventurer'
        membership.active_adventurer_id = aid
        membership.claim_reservation_id = None
        membership.claim_token = None
        membership.reconcile_absent_since = None
        membership.updated_at = now or _portable_utc_now()
        return membership

    @staticmethod
    def abort_active_slot_claim(
        user_id: int,
        *,
        realm_code: str,
        reservation_id: int,
        claim_token: str,
    ) -> bool:
        """Release only the exact still-pending claim; active rows are untouched."""
        uid = int(user_id)
        realm = S1RealmService._validate_realm_code(realm_code)
        if S1RealmRepository.lock_user(uid) is None:
            return False
        membership = S1RealmRepository.get_membership(uid, for_update=True)
        if membership is None:
            return False
        if (
            S1RealmService._validate_slot_state(membership.slot_state) == SLOT_PENDING
            and membership.realm_code == realm
            and membership.claim_reservation_id == int(reservation_id)
            and secrets.compare_digest(
                str(membership.claim_token or ''),
                str(claim_token or ''),
            )
        ):
            S1RealmRepository.delete_membership(membership)
            return True
        return False

    @staticmethod
    def release_active_slot(
        user_id: int,
        *,
        realm_code: str,
        adventurer_id: int,
    ) -> bool:
        """Release the exact terminal/archived adventurer's active slot."""
        uid = int(user_id)
        realm = S1RealmService._validate_realm_code(realm_code)
        if S1RealmRepository.lock_user(uid) is None:
            return False
        membership = S1RealmRepository.get_membership(uid, for_update=True)
        if membership is None:
            return False
        if (
            S1RealmService._validate_slot_state(membership.slot_state) == SLOT_ACTIVE
            and membership.realm_code == realm
            and membership.active_adventurer_id == int(adventurer_id)
        ):
            S1RealmRepository.delete_membership(membership)
            return True
        return False

    @staticmethod
    def reconcile_active_slots(
        *,
        realm_code: str,
        slots: list[dict[str, int]],
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Replace one Runtime's finalized inventory without touching fresh claims."""
        realm = S1RealmService._validate_realm_code(realm_code)
        if not isinstance(slots, list):
            raise S1RealmAssignmentError('全局角色槽位对账清单无效')
        inventory: dict[int, int] = {}
        for item in slots:
            if not isinstance(item, dict) or set(item) != {'user_id', 'adventurer_id'}:
                raise S1RealmAssignmentError('全局角色槽位对账清单无效')
            uid = int(item['user_id'])
            aid = int(item['adventurer_id'])
            if uid <= 0 or aid <= 0 or (uid in inventory and inventory[uid] != aid):
                raise S1RealmAssignmentError('全局角色槽位对账清单存在冲突')
            inventory[uid] = aid

        existing_realm_rows = S1RealmRepository.list_memberships_for_realm(realm)
        user_ids = sorted(set(inventory) | {row.user_id for row in existing_realm_rows})
        locked_users = S1RealmRepository.lock_users(user_ids)
        locked_user_ids = {int(user.id) for user in locked_users}
        if any(uid not in locked_user_ids for uid in inventory):
            raise S1RealmAssignmentError(
                '全局角色槽位对账包含不存在的用户'
            )
        rows = S1RealmRepository.list_memberships_for_users(
            user_ids,
            for_update=True,
        )
        by_user = {int(row.user_id): row for row in rows}
        if any(uid in by_user and by_user[uid].realm_code != realm for uid in inventory):
            raise S1RealmActiveSlotError(
                '二区对账发现账号已被一区存活角色占用，Runtime 将保持未就绪'
            )

        reconciled_at = now or _portable_utc_now()
        inserted = 0
        updated = 0
        released = 0
        preserved_pending = 0
        preserved_active = 0
        for uid, aid in inventory.items():
            row = by_user.get(uid)
            if row is None:
                row = AdventurerS1RealmMembership(
                    user_id=uid,
                    realm_code=realm,
                    assignment_reason='runtime_reconcile',
                    slot_state=SLOT_ACTIVE,
                    active_adventurer_id=aid,
                    created_at=reconciled_at,
                    updated_at=reconciled_at,
                )
                S1RealmRepository.add_membership(row)
                by_user[uid] = row
                inserted += 1
                continue
            if (
                S1RealmService._validate_slot_state(row.slot_state) == SLOT_ACTIVE
                and row.realm_code == realm
                and row.assignment_reason == 'runtime_reconcile'
                and row.active_adventurer_id == aid
                and row.claim_reservation_id is None
                and row.claim_token is None
                and row.reconcile_absent_since is None
            ):
                # The complete inventory is a level-triggered assertion.  A
                # stable snapshot must not create one row version per player
                # every minute merely to prove the same state again.
                continue
            row.slot_state = SLOT_ACTIVE
            row.assignment_reason = 'runtime_reconcile'
            row.active_adventurer_id = aid
            row.claim_reservation_id = None
            row.claim_token = None
            row.reconcile_absent_since = None
            row.updated_at = reconciled_at
            updated += 1

        for uid, row in list(by_user.items()):
            if row.realm_code != realm or uid in inventory:
                continue
            if (
                S1RealmService._validate_slot_state(row.slot_state) == SLOT_PENDING
                and not S1RealmService._pending_is_expired(row, now=reconciled_at)
            ):
                preserved_pending += 1
                continue
            if S1RealmService._validate_slot_state(row.slot_state) == SLOT_ACTIVE:
                # A full-inventory query can race a just-created local role: an
                # older empty snapshot must not delete a newer central finalize.
                # Consecutive absent snapshots eventually release death/archive.
                absent_since = row.reconcile_absent_since
                if (
                    absent_since is None
                    or (
                        row.updated_at is not None
                        and row.updated_at > absent_since
                    )
                ):
                    # During a rolling deployment an older central worker can
                    # refresh updated_at after this newer field was marked but
                    # cannot clear the field itself.  Treat that refresh as a
                    # legacy reappearance receipt and begin a new grace window.
                    row.reconcile_absent_since = reconciled_at
                    row.updated_at = reconciled_at
                    updated += 1
                    preserved_active += 1
                    continue
                if (
                    absent_since + S1RealmService._active_absence_grace()
                    > reconciled_at
                ):
                    preserved_active += 1
                    continue
            S1RealmRepository.delete_membership(row)
            released += 1
        return {
            'inserted': inserted,
            'updated': updated,
            'released': released,
            'preserved_pending': preserved_pending,
            'preserved_active': preserved_active,
            'active_count': len(inventory),
        }

    @staticmethod
    def _runtime_active_slot_client():
        client = current_app.extensions.get('s1_active_slot_client')
        if client is None:
            raise S1Region2AdmissionUnavailableError('二区全局角色槽位服务未就绪')
        return client

    @staticmethod
    def claim_remote_active_slot(
        user_id: int,
        *,
        reservation_id: int,
    ) -> str:
        if S1RealmService.current_runtime_realm() != REALM_2:
            raise S1RealmAssignmentError('只有二区 Runtime 可以远程认领角色槽位')
        client = S1RealmService._runtime_active_slot_client()
        try:
            result = client.claim_active_slot(
                user_id=int(user_id),
                reservation_id=int(reservation_id),
            )
        except S1RealmActiveSlotError as conflict:
            # A region-2 active row can briefly outlive a just-committed local
            # death.  Reconcile once before classifying the queue entry as a
            # permanent cross-realm loser.  Fresh absent rows remain protected
            # and therefore retryable; a real region-1 conflict survives the
            # reconciliation and is returned by the second claim below.
            reconciliation = S1RealmService.reconcile_remote_active_slots()
            if reconciliation['preserved_active'] > 0:
                raise S1Region2AdmissionUnavailableError(
                    '二区正在确认刚结束的角色状态，请稍后重试抽取'
                ) from conflict
            result = client.claim_active_slot(
                user_id=int(user_id),
                reservation_id=int(reservation_id),
            )
        token = str((result or {}).get('claim_token') or '')
        if (
            len(token) != 32
            or any(char not in '0123456789abcdef' for char in token)
        ):
            raise S1Region2AdmissionUnavailableError('二区全局角色槽位回执无效')
        return token

    @staticmethod
    def finalize_remote_active_slot(
        user_id: int,
        *,
        reservation_id: int,
        claim_token: str,
        adventurer_id: int,
    ) -> None:
        S1RealmService._runtime_active_slot_client().finalize_active_slot(
            user_id=int(user_id),
            reservation_id=int(reservation_id),
            claim_token=str(claim_token),
            adventurer_id=int(adventurer_id),
        )

    @staticmethod
    def abort_remote_active_slot_claim(
        user_id: int,
        *,
        reservation_id: int,
        claim_token: str,
    ) -> None:
        S1RealmService._runtime_active_slot_client().abort_active_slot_claim(
            user_id=int(user_id),
            reservation_id=int(reservation_id),
            claim_token=str(claim_token),
        )

    @staticmethod
    def release_remote_active_slot(
        user_id: int,
        *,
        adventurer_id: int,
    ) -> None:
        S1RealmService._runtime_active_slot_client().release_active_slot(
            user_id=int(user_id),
            adventurer_id=int(adventurer_id),
        )

    @staticmethod
    def reconcile_remote_active_slots() -> dict[str, int]:
        """Send a detached full region-2 inventory to the central authority."""
        if S1RealmService.current_runtime_realm() != REALM_2:
            return {'active_count': 0, 'skipped': 1}
        from db_repository.s1_role_admission_lock import (
            release_s1_role_admission_lock,
            try_acquire_s1_role_admission_lock,
        )

        admission_lock = try_acquire_s1_role_admission_lock()
        if admission_lock is None:
            raise S1Region2AdmissionUnavailableError(
                '二区角色创建或对账正在进行，本轮暂不回收全局角色槽位'
            )
        try:
            return S1RealmService._reconcile_remote_active_slots_locked()
        finally:
            release_s1_role_admission_lock(admission_lock)

    @staticmethod
    def _reconcile_remote_active_slots_locked() -> dict[str, int]:
        # The same database lock covers admission's claim -> local commit ->
        # finalize. Keep it from BEFORE this snapshot through its central reply:
        # an absent role then cannot belong to a still-running creation task,
        # even after the central pending TTL expires.
        local_slots = S1RealmRepository.list_active_player_adventurer_slots()
        slots = []
        for user_id, adventurer_id, realm_code in local_slots:
            if S1RealmService._validate_realm_code(realm_code) != REALM_2:
                raise S1RealmAssignmentError(
                    '二区 Runtime 存在非二区存活角色，拒绝对账'
                )
            slots.append({
                'user_id': user_id,
                'adventurer_id': adventurer_id,
            })
        from db_repository import db_manager

        db_manager.remove()
        result: Any = S1RealmService._runtime_active_slot_client().reconcile_active_slots(
            slots=slots,
        )
        expected_keys = {
            'inserted',
            'updated',
            'released',
            'preserved_pending',
            'preserved_active',
            'active_count',
        }
        if (
            not isinstance(result, dict)
            or set(result) != expected_keys
            or any(type(result[key]) is not int or result[key] < 0 for key in expected_keys)
            or result['active_count'] != len(slots)
        ):
            raise S1Region2AdmissionUnavailableError('二区全局角色槽位对账回执无效')
        return dict(result)

    @staticmethod
    def require_matching_realm(
        membership: AdventurerS1RealmMembership,
        realm_code: str,
    ) -> str:
        expected = S1RealmService._validate_realm_code(membership.realm_code)
        actual = S1RealmService._validate_realm_code(realm_code)
        if actual != expected:
            raise S1RealmAssignmentError('预约或冒险者的区服与全局角色槽位不一致')
        return expected

    @staticmethod
    def require_free_active_slot(
        user_id: int,
        *,
        exclude_adventurer_id: int | None = None,
    ) -> None:
        """Retain the local DB invariant in addition to the central global row."""
        if S1RealmRepository.count_active_player_adventurers(
            int(user_id),
            exclude_adventurer_id=exclude_adventurer_id,
        ) >= 1:
            raise S1RealmActiveSlotError('每名玩家在一二区合计只能有 1 个存活冒险者')
