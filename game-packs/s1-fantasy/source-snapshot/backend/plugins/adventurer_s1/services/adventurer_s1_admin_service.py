"""深渊酒馆 S1 管理员运维：不活跃检测通知 + 封存清退 + 强制中断建筑。"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    S1PlayerVisibleError, player_operation_failure,
)
import json
import logging
import random
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from db_repository.s1_realm_repository import S1RealmRepository
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1CurrencyLog,
    AdventurerS1DormRoom,
    AdventurerS1Equipment,
    AdventurerS1Reservation,
    AdventurerS1WarehouseItem,
)
from utils.pagination import build_paginated_response
from utils.time_utils import to_utc_iso
from plugins.adventurer_s1.services.s1_realm_service import (
    S1RealmAssignmentError,
    S1RealmService,
)

logger = logging.getLogger(__name__)

_INACTIVE_DIGEST_CONFIG_KEY = 'adventurer_s1_inactive_admin_digest'
_ADMIN_GOLD_ADD_SOURCE_TYPE = 'admin_gold_add'
_ADMIN_GOLD_ADD_RELATED_TYPE = 'idempotency_key'
_ADMIN_GOLD_ADD_REASON_PREFIX = '管理员增加金币：'
_ADMIN_GOLD_ADD_REASON_MAX_LENGTH = 180
_ADMIN_GOLD_ADD_IDEMPOTENCY_KEY_MAX_LENGTH = 50


class AdventurerS1AdminService:

    @staticmethod
    def admin_switch_all_players_to_manual(admin_user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """将当前运行区服全部在册玩家的自动匹配偏好一次性改为手动。"""
        from db_repository.s1_party_preference_repository import S1PartyPreferenceRepository
        from services.admin_operation_audit_service import AdminOperationAuditService

        realm_code = S1RealmService.current_runtime_realm()
        try:
            with db_manager.transaction():
                updated_count = S1PartyPreferenceRepository.switch_all_player_auto_to_manual(realm_code)
                AdminOperationAuditService.annotate_current_operation(
                    action_code='s1_players_switch_to_manual',
                    target_type='s1_realm',
                    target_id=realm_code,
                    reason=f'批量将 {updated_count} 名玩家冒险者从自动匹配切换为手动',
                )
        except Exception:
            logger.exception(
                'S1 批量切换手动匹配失败 admin_user_id=%s realm_code=%s',
                admin_user_id, realm_code,
            )
            return None, '批量切换失败，请稍后重试'

        # 提交完成后再清缓存和推送；推送失败不改变已落库的操作结果。
        if updated_count:
            try:
                from plugins.adventurer_s1.services.adventurer_s1_service import (
                    invalidate_adventurer_detail_cache,
                )
                invalidate_adventurer_detail_cache()
            except Exception:
                logger.exception('S1 批量切换后清理详情缓存失败 realm_code=%s', realm_code)
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio
                s1_socketio.emit('adventurer_changed', {
                    'action': 'party_preferences_changed',
                    'reason': 'admin_switch_all_players_to_manual',
                })
            except Exception:
                logger.exception('S1 批量切换后推送刷新失败 realm_code=%s', realm_code)

        logger.info(
            'S1 批量切换手动匹配 admin_user_id=%s realm_code=%s updated_count=%s',
            admin_user_id, realm_code, updated_count,
        )
        return {
            'realm_code': realm_code,
            'updated_count': updated_count,
            'party_preference': 'manual',
        }, None

    @staticmethod
    def admin_add_gold(
        adventurer_id: int,
        amount,
        reason,
        idempotency_key,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """只增不减地补充金币；重复幂等键只回放首次入账结果。"""
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
            S1CurrencyService,
        )

        normalized_amount, amount_err = S1CurrencyService._validate_amount(amount)
        if amount_err:
            return None, amount_err
        if normalized_amount <= 0:
            return None, '增加金币金额必须大于 0'

        if reason is None:
            return None, '请填写增加金币原因'
        if not isinstance(reason, str):
            return None, 'reason 必须是字符串'
        reason_text = reason.strip()
        if not reason_text:
            return None, '请填写增加金币原因'
        if len(reason_text) > _ADMIN_GOLD_ADD_REASON_MAX_LENGTH:
            return None, f'增加金币原因不能超过 {_ADMIN_GOLD_ADD_REASON_MAX_LENGTH} 个字符'

        if not isinstance(idempotency_key, str):
            return None, '缺少 Idempotency-Key'
        request_key = idempotency_key.strip()
        if not request_key:
            return None, '缺少 Idempotency-Key'
        if len(request_key) > _ADMIN_GOLD_ADD_IDEMPOTENCY_KEY_MAX_LENGTH:
            return None, (
                'Idempotency-Key 不能超过 '
                f'{_ADMIN_GOLD_ADD_IDEMPOTENCY_KEY_MAX_LENGTH} 个字符'
            )

        description = f'{_ADMIN_GOLD_ADD_REASON_PREFIX}{reason_text}'
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not adv:
                    return None, '冒险者不存在'

                existing_log = (
                    AdventurerS1CurrencyLog.query
                    .filter_by(
                        adventurer_id=adventurer_id,
                        currency_code='gold',
                        source_type=_ADMIN_GOLD_ADD_SOURCE_TYPE,
                        related_id=request_key,
                        related_type=_ADMIN_GOLD_ADD_RELATED_TYPE,
                    )
                    .order_by(AdventurerS1CurrencyLog.id.asc())
                    .first()
                )
                if existing_log is not None:
                    same_request = (
                        round(float(existing_log.change_amount), 2) == normalized_amount
                        and existing_log.description == description
                    )
                    if not same_request:
                        return None, '该 Idempotency-Key 已用于另一笔增加金币请求'

                    current_balance, balance_err = S1CurrencyService.get_balance(
                        adventurer_id,
                        'gold',
                    )
                    if balance_err:
                        raise RuntimeError(balance_err)
                    original_balance_after = round(float(existing_log.balance_after), 2)
                    return {
                        'adventurer_id': adv.id,
                        'adventurer_name': adv.name,
                        'added_amount': normalized_amount,
                        'balance_before': round(
                            original_balance_after - normalized_amount,
                            2,
                        ),
                        'balance_after': original_balance_after,
                        'current_balance': round(float(current_balance), 2),
                        'reason': reason_text,
                        'idempotency_key': request_key,
                        'idempotent_replay': True,
                    }, None

                balance_before, balance_err = S1CurrencyService.get_balance(
                    adventurer_id,
                    'gold',
                )
                if balance_err:
                    raise RuntimeError(balance_err)

                balances, add_err = S1CurrencyService.apply_changes_in_transaction(
                    'gold',
                    [{
                        'adventurer_id': adventurer_id,
                        'change_amount': normalized_amount,
                        'source_type': _ADMIN_GOLD_ADD_SOURCE_TYPE,
                        'related_id': request_key,
                        'related_type': _ADMIN_GOLD_ADD_RELATED_TYPE,
                        'description': description,
                        'silent': False,
                    }],
                )
                if add_err:
                    raise RuntimeError(add_err)
                balance_after = round(float(balances[adventurer_id]), 2)

                return {
                    'adventurer_id': adv.id,
                    'adventurer_name': adv.name,
                    'added_amount': normalized_amount,
                    'balance_before': round(float(balance_before), 2),
                    'balance_after': balance_after,
                    'current_balance': balance_after,
                    'reason': reason_text,
                    'idempotency_key': request_key,
                    'idempotent_replay': False,
                }, None
        except Exception:
            logger.exception(
                'S1 管理员增加金币失败 adventurer_id=%s idempotency_key=%s',
                adventurer_id,
                request_key,
            )
            return None, '增加金币失败，请稍后重试'

    @staticmethod
    def _inactive_cutoff() -> datetime:
        hours = int(current_app.config.get('ADVENTURER_S1_INACTIVE_ALERT_HOURS', 24))
        return _portable_utc_now() - timedelta(hours=hours)

    @staticmethod
    def _last_owner_activity_at(adv: AdventurerS1, owner_login_map: dict):
        """返回玩家最近一次可证明的登录或 S1 手动操作时间。"""
        if not adv.owner_id:
            return None
        candidates = [
            value for value in (
                adv.last_owner_action_at,
                owner_login_map.get(adv.owner_id),
            )
            if value is not None
        ]
        return max(candidates) if candidates else None

    @staticmethod
    def _is_adventurer_inactive(adv: AdventurerS1, owner_login_map: dict, cutoff: datetime) -> bool:
        """判定冒险者是否超过不活跃窗口。

        玩家自建冒险者看最近登录或 last_owner_action_at（手动操作，不含自动下本）；
        无 owner 的系统 NPC 不参与不活跃检测（scheduler 会污染 updated_at）。
        """
        if adv.owner_id:
            last_activity = AdventurerS1AdminService._last_owner_activity_at(
                adv,
                owner_login_map,
            )
            return not last_activity or last_activity < cutoff
        return False

    @staticmethod
    def _tavern_roster_query():
        return AdventurerS1.query.filter(
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )

    @staticmethod
    def list_inactive_adventurers(
        page: int = 1,
        per_page: int = 50,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """列出酒馆编制内、超过不活跃窗口的冒险者（管理员）。"""
        from models.user import User

        cutoff = AdventurerS1AdminService._inactive_cutoff()
        hours = int(current_app.config.get('ADVENTURER_S1_INACTIVE_ALERT_HOURS', 24))

        roster = AdventurerS1AdminService._tavern_roster_query().all()
        if not roster:
            return build_paginated_response([], 0, page, per_page), None

        owner_ids = {adv.owner_id for adv in roster if adv.owner_id}
        login_map = {}
        user_meta = {}
        if owner_ids:
            rows = (
                User.query.with_entities(
                    User.id, User.last_login_at, User.username, User.nickname,
                )
                .filter(User.id.in_(owner_ids), User.is_deleted == False)  # noqa: E712
                .all()
            )
            for uid, last_login_at, username, nickname in rows:
                login_map[uid] = last_login_at
                user_meta[uid] = {
                    'username': username,
                    'nickname': nickname,
                }

        inactive_rows = [
            adv for adv in roster
            if AdventurerS1AdminService._is_adventurer_inactive(adv, login_map, cutoff)
        ]
        inactive_rows.sort(
            key=lambda a: (
                AdventurerS1AdminService._last_owner_activity_at(a, login_map)
                or datetime.min
            ),
            reverse=False,
        )

        total = len(inactive_rows)
        start = (page - 1) * per_page
        page_rows = inactive_rows[start:start + per_page]

        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
            invalidate_dormitory_cache,
        )
        building_info_map = AdventurerS1Service.build_adventurer_building_info_map(page_rows)

        items = []
        for adv in page_rows:
            owner = user_meta.get(adv.owner_id, {}) if adv.owner_id else {}
            last_login = login_map.get(adv.owner_id) if adv.owner_id else None
            last_action = adv.last_owner_action_at if adv.owner_id else None
            inactive_since = AdventurerS1AdminService._last_owner_activity_at(
                adv,
                login_map,
            )
            items.append({
                'id': adv.id,
                'name': adv.name,
                'level': adv.level,
                'status': adv.status,
                'owner_id': adv.owner_id,
                'owner_username': owner.get('username'),
                'owner_nickname': owner.get('nickname'),
                'last_login_at': to_utc_iso(last_login) if last_login else None,
                'last_owner_action_at': to_utc_iso(last_action) if last_action else None,
                'inactive_since': to_utc_iso(inactive_since) if inactive_since else None,
                'dorm_room_id': adv.dorm_room_id,
                'bed_index': adv.bed_index,
                'building_info': building_info_map.get(adv.id),
            })

        result = build_paginated_response(items, total, page, per_page)
        result['inactive_hours'] = hours
        result['inactive_count'] = total
        return result, None

    @staticmethod
    def _clear_tavern_seat(adventurer_id: int) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
            AdventurerS1TavernSeatService,
        )
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
            AdventurerS1TeaHouseSeatService,
        )
        from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
            AdventurerS1HotSpringSeatService,
        )

        AdventurerS1TavernSeatService.clear_seats_for_adventurer(adventurer_id)
        AdventurerS1TeaHouseSeatService.clear_seats_for_adventurer(adventurer_id)
        AdventurerS1HotSpringSeatService.clear_seats_for_adventurer(adventurer_id)

    @staticmethod
    def _cancel_pending_bed_swaps(adventurer_id: int) -> None:
        from plugins.adventurer_s1.models import AdventurerS1BedSwapRequest

        now = _portable_utc_now()
        AdventurerS1BedSwapRequest.query.filter(
            AdventurerS1BedSwapRequest.status == 'pending',
            or_(
                AdventurerS1BedSwapRequest.requester_adv_id == adventurer_id,
                AdventurerS1BedSwapRequest.target_adv_id == adventurer_id,
            ),
        ).update({
            'status': 'cancelled',
            'updated_at': now,
        }, synchronize_session=False)

    @staticmethod
    def _cancel_active_market_listings(adventurer_id: int) -> None:
        from plugins.adventurer_s1.models import AdventurerS1MarketListing
        from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService

        listing_ids = [
            row[0] for row in (
                AdventurerS1MarketListing.query.with_entities(AdventurerS1MarketListing.id)
                .filter(
                    AdventurerS1MarketListing.seller_adventurer_id == adventurer_id,
                    AdventurerS1MarketListing.status == 'active',
                ).all()
            )
        ]
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        owner_id = adv.owner_id if adv else None
        if not owner_id:
            return
        for listing_id in listing_ids:
            S1MarketService.cancel_listing(owner_id, listing_id)
        from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketBuyOrderService
        S1MarketBuyOrderService.cancel_adventurer_activity(adventurer_id)

    @staticmethod
    def admin_archive_adventurer(
        adventurer_id: int,
        admin_user_id: int,
        reason: Optional[str] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """封存清退：保留档案盒全部数据，清出酒馆编制并释放床位。"""
        from db_repository.s1_role_admission_lock import (
            release_s1_role_admission_lock,
            try_acquire_s1_role_admission_lock,
        )

        admission_lock = try_acquire_s1_role_admission_lock()
        if admission_lock is None:
            return None, '已有角色创建或对账任务进行中，请稍后再试'
        try:
            return AdventurerS1AdminService._archive_adventurer_locked(
                adventurer_id, admin_user_id, reason=reason,
            )
        finally:
            release_s1_role_admission_lock(admission_lock)

    @staticmethod
    def _archive_adventurer_locked(
        adventurer_id: int,
        admin_user_id: int,
        reason: Optional[str] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.adventurer_s1_party_service import leave_party
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
            invalidate_dormitory_cache,
        )

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if adv.status == 'archived':
            return None, '该冒险者已封存清退'
        if adv.status == 'dead':
            return None, '已死亡的冒险者不能封存清退'
        if adv.status == 'exploring':
            return None, '探险中的冒险者不能封存清退，请先解散小队或等待探险结束'

        now = _portable_utc_now()
        reason_text = (reason or '').strip()[:200] or None

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if adv and adv.owner_id:
            AdventurerS1AdminService._cancel_active_market_listings(adventurer_id)

        party_leave_info = None
        try:
            with db_manager.transaction():
                if adv.status == 'in_party':
                    ok, err, party_leave_info = leave_party(adv, auto_commit=False)
                    if not ok:
                        return None, err or '离队失败，无法封存清退'
                    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)

                if adv.building_instance_id:
                    inst = adv.building_instance
                    if inst and adv.status == 'building':
                        AdventurerS1Service._release_building_occupants(inst, now)
                    else:
                        AdventurerS1Service.leave_rest(adv, 'resting', now)
                        adv.building_instance_id = None
                        adv.building_assigned_at = None
                        adv.building_usage_extra = None

                if adv.status in AdventurerS1.DISPATCHABLE_STATUSES:
                    AdventurerS1Service.leave_rest(adv, 'archived', now)
                else:
                    adv.status = 'archived'

                adv.archived_from_dorm_room_id = adv.dorm_room_id
                adv.archived_from_bed_index = adv.bed_index
                adv.dorm_room_id = None
                adv.bed_index = None
                adv.last_bed_swap_at = None
                adv.rest_start_time = None
                adv.recovery_time = None
                adv.archived_at = now
                adv.archived_by_user_id = admin_user_id
                adv.archive_reason = reason_text

                if (
                    adv.is_player_created
                    and adv.owner_id is not None
                    and S1RealmService.current_runtime_realm() == 'region_1'
                ):
                    S1RealmService.release_active_slot(
                        adv.owner_id,
                        realm_code='region_1',
                        adventurer_id=adventurer_id,
                    )

                AdventurerS1AdminService._clear_tavern_seat(adventurer_id)
                AdventurerS1AdminService._cancel_pending_bed_swaps(adventurer_id)
        except Exception as e:
            return None, f'封存清退失败：{e}'

        if party_leave_info:
            AdventurerS1Service._publish_party_leave(
                adventurer_id,
                party_leave_info,
                reason='admin_archive',
                emit_adventurer=False,
            )
        invalidate_dormitory_cache()

        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': 'archived',
                'reason': 'admin_archive',
                'ids': [adventurer_id],
            }, namespace='/')
        except Exception:
            pass

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        response = {
            'adventurer_id': adventurer_id,
            'status': adv.status,
            'archived_at': to_utc_iso(adv.archived_at) if adv.archived_at else None,
            'capacity': AdventurerS1Service.get_draw_capacity_stats(),
        }
        if (
            adv.is_player_created
            and adv.owner_id is not None
            and S1RealmService.current_runtime_realm() == 'region_2'
        ):
            owner_id = int(adv.owner_id)
            db_manager.remove()
            try:
                S1RealmService.release_remote_active_slot(
                    owner_id,
                    adventurer_id=adventurer_id,
                )
            except Exception:
                current_app.logger.exception(
                    '二区角色封存后释放中央槽位失败 adv=%s',
                    adventurer_id,
                )
        return response, None

    @staticmethod
    def admin_unarchive_adventurer(
        adventurer_id: int,
        admin_user_id: int,
        *,
        reservation_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """解封清退冒险者；抽取恢复时与预约 ``drawn`` 原子提交。"""
        from db_repository.s1_role_admission_lock import (
            release_s1_role_admission_lock,
            try_acquire_s1_role_admission_lock,
        )

        admission_lock = try_acquire_s1_role_admission_lock()
        if admission_lock is None:
            return None, '已有角色创建或对账任务进行中，请稍后再试'
        try:
            return AdventurerS1AdminService._unarchive_adventurer_locked(
                adventurer_id,
                admin_user_id,
                reservation_id=reservation_id,
            )
        finally:
            release_s1_role_admission_lock(admission_lock)

    @staticmethod
    def _unarchive_adventurer_locked(
        adventurer_id: int,
        admin_user_id: int,
        *,
        reservation_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
            invalidate_dormitory_cache,
        )

        reservation = None
        if reservation_id is not None:
            reservation = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, int(reservation_id))
            if not reservation:
                return None, '解封预约不存在'
            if reservation.status != 'pending':
                return None, '解封预约已不在待抽取状态'
            if reservation.restore_adventurer_id != int(adventurer_id):
                return None, '解封预约与冒险者不匹配'

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if adv.status != 'archived':
            return None, '只有已封存清退的冒险者才能解封'
        target_room_id = None
        target_bed_index = None
        target_room_name = None
        used_original_bed = False
        original_slot = None
        candidates = []
        room_map = {}

        if adv.archived_from_dorm_room_id is not None and adv.archived_from_bed_index is not None:
            room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, adv.archived_from_dorm_room_id)
            if (
                room
                and AdventurerS1Service._room_allows_adventurer(room, adv)
                and 0 <= adv.archived_from_bed_index < room.capacity
            ):
                original_slot = (
                    adv.archived_from_dorm_room_id,
                    adv.archived_from_bed_index,
                )
                candidates.append(original_slot)
                room_map[room.id] = room

        fallback_beds = AdventurerS1Service._collect_free_bed_slots()
        no_bed_error = '当前没有普通空床位，无法解封'
        random.shuffle(fallback_beds)
        candidates.extend(slot for slot in fallback_beds if slot != original_slot)
        if not candidates:
            return None, no_bed_error

        now = _portable_utc_now()
        expected_owner_id = (
            reservation.user_id if reservation is not None else adv.owner_id
        )
        runtime_realm = S1RealmService.current_runtime_realm()
        try:
            if S1RealmService._validate_realm_code(adv.realm_code) != runtime_realm:
                return None, '封存冒险者不属于当前区服'
        except S1RealmAssignmentError as exc:
            return None, str(exc)
        claim_reference_id = int(reservation_id or adventurer_id)
        remote_claim_token = None
        if adv.is_player_created and expected_owner_id is not None and runtime_realm == 'region_2':
            db_manager.remove()
            try:
                remote_claim_token = S1RealmService.claim_remote_active_slot(
                    expected_owner_id,
                    reservation_id=claim_reference_id,
                )
            except S1RealmAssignmentError as exc:
                return None, str(exc)

        local_claim_token = None
        try:
            with db_manager.transaction():
                membership = None
                if (
                    expected_owner_id is not None
                    and (reservation is not None or adv.is_player_created)
                    and runtime_realm == 'region_1'
                ):
                    membership = S1RealmService.claim_active_slot(
                        expected_owner_id,
                        realm_code=runtime_realm,
                        reservation_id=claim_reference_id,
                        assignment_reason='unarchive_claim',
                    )
                    local_claim_token = str(membership.claim_token or '')
                if reservation_id is not None:
                    reservation = S1RealmRepository.lock_reservation(
                        int(reservation_id),
                    )
                    if (
                        not reservation
                        or reservation.status != 'pending'
                        or reservation.user_id != expected_owner_id
                        or reservation.restore_adventurer_id != int(adventurer_id)
                    ):
                        raise S1PlayerVisibleError('解封预约状态或归属已变化')
                    if S1RealmService._validate_realm_code(
                        reservation.realm_code
                    ) != runtime_realm:
                        raise S1PlayerVisibleError('解封预约不属于当前区服')
                adv = S1RealmRepository.lock_adventurer(adventurer_id)
                if not adv or adv.status != 'archived':
                    raise S1PlayerVisibleError('冒险者已不是封存状态')
                if adv.owner_id != expected_owner_id:
                    raise S1PlayerVisibleError('冒险者归属已变化，请重试')
                if adv.is_player_created and expected_owner_id is not None:
                    S1RealmService.require_free_active_slot(
                        expected_owner_id,
                        exclude_adventurer_id=adventurer_id,
                    )
                for room_id, bed_index in candidates:
                    if AdventurerS1Service._claim_bed_slot(
                        adv, room_id, bed_index,
                    ):
                        target_room_id = room_id
                        target_bed_index = bed_index
                        break
                if target_room_id is None or target_bed_index is None:
                    raise S1PlayerVisibleError(no_bed_error)
                adv.status = 'resting'
                adv.rest_start_time = now
                adv.stamina_updated_at = now
                adv.hp_updated_at = now
                adv.recovery_time = None
                adv.archived_at = None
                adv.archived_by_user_id = None
                adv.archive_reason = None
                adv.archived_from_dorm_room_id = None
                adv.archived_from_bed_index = None
                used_original_bed = (
                    original_slot == (target_room_id, target_bed_index)
                )
                if reservation is not None:
                    reservation.status = 'drawn'
                    reservation.reject_reason = None
                if membership is not None:
                    S1RealmService.finalize_active_slot(
                        expected_owner_id,
                        realm_code=runtime_realm,
                        reservation_id=claim_reference_id,
                        claim_token=local_claim_token,
                        adventurer_id=adventurer_id,
                    )
        except Exception as e:
            if remote_claim_token:
                try:
                    S1RealmService.abort_remote_active_slot_claim(
                        expected_owner_id,
                        reservation_id=claim_reference_id,
                        claim_token=remote_claim_token,
                    )
                except Exception:
                    current_app.logger.warning(
                        '二区解封失败后释放中央槽位失败 adv=%s',
                        adventurer_id,
                        exc_info=True,
                    )
            return None, player_operation_failure(e, '解封失败')

        if remote_claim_token:
            try:
                S1RealmService.finalize_remote_active_slot(
                    expected_owner_id,
                    reservation_id=claim_reference_id,
                    claim_token=remote_claim_token,
                    adventurer_id=adventurer_id,
                )
            except Exception:
                current_app.logger.exception(
                    '二区解封已提交但中央槽位 finalize 暂时失败 adv=%s',
                    adventurer_id,
                )

        room = room_map.get(target_room_id)
        if room is None:
            room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, target_room_id)
        target_room_name = room.name if room else f'宿舍 #{target_room_id}'

        invalidate_dormitory_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': 'unarchived',
                'reason': 'admin_unarchive',
                'ids': [adventurer_id],
            }, namespace='/')
        except Exception:
            pass

        return {
            'adventurer_id': adventurer_id,
            'status': adv.status,
            'dorm_room_id': adv.dorm_room_id,
            'bed_index': adv.bed_index,
            'reservation_id': reservation_id,
            'message': (
                f'{adv.name} 已解封并回到原宿舍「{target_room_name}」第 {adv.bed_index + 1} 号床位'
                if used_original_bed
                else f'{adv.name} 已解封；原床位不可用，已随机安排到「{target_room_name}」第 {adv.bed_index + 1} 号床位'
            ),
            'capacity': AdventurerS1Service.get_draw_capacity_stats(),
        }, None

    @staticmethod
    def admin_force_evict_from_building(
        adventurer_id: int,
        admin_user_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员强制中断建筑/座位占用，将冒险者送回宿舍休息态。

        自动判断场景：
        - 帮工建造中（建筑未建成）→ 委托 recall_from_building
        - 功能使用中（建筑已建成）→ 结算奖励 + 释放
        - 茶舍/酒馆座位 → 清除座位
        """
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'

        # ── 场景 1：标准建筑占用（status='building'） ──
        if adv.status == 'building' and adv.building_instance_id:
            inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, adv.building_instance_id)
            if not inst:
                return None, '建筑实例不存在'

            # 帮工建造中：委托已有的召回逻辑
            if inst.status != 'built':
                data, err = AdventurerS1Service.recall_from_building(
                    adventurer_id, admin_user_id, is_admin=True,
                )
                if err:
                    return None, err
                return data, None

            # 功能使用中（已建成建筑）：强制遣返，不结算奖励
            try:
                with db_manager.transaction():
                    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, with_for_update=True)
                    if not adv:
                        raise RuntimeError('冒险者不存在（并发冲突）')
                    if adv.status != 'building' or adv.building_instance_id != inst.id:
                        raise RuntimeError('冒险者已不在该建筑中')

                    AdventurerS1Service._flush_building_rest_like_recovery(adv)
                    AdventurerS1Service.enter_rest(adv)
                    adv.building_instance_id = None
                    adv.building_assigned_at = None
                    adv.building_usage_extra = None
            except Exception as e:
                return None, f'强制中断失败：{e}'

            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {
                    'action': 'admin_force_evict',
                    'reason': 'admin_force_evict',
                    'ids': [adventurer_id],
                }, namespace='/')
            except Exception:
                pass

            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            return {
                'adventurer': adv.to_dict(is_admin=True),
                'building': inst.to_dict(),
            }, None

        # ── 场景 2：茶舍座位 ──
        from plugins.adventurer_s1.models import AdventurerS1TeaHouseSeat
        tea_seat = AdventurerS1TeaHouseSeat.query.filter_by(
            adventurer_id=adventurer_id,
        ).first()
        if tea_seat:
            from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
                AdventurerS1TeaHouseSeatService,
            )
            AdventurerS1TeaHouseSeatService.clear_seats_for_adventurer(adventurer_id)
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            return {
                'adventurer': adv.to_dict(is_admin=True),
                'evicted_from': 'tea_house',
            }, None

        # ── 场景 2.5：温泉座位 ──
        from plugins.adventurer_s1.models import AdventurerS1HotSpringSeat
        hot_spring_seat = AdventurerS1HotSpringSeat.query.filter_by(
            adventurer_id=adventurer_id,
        ).first()
        if hot_spring_seat:
            from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
                AdventurerS1HotSpringSeatService,
            )
            AdventurerS1HotSpringSeatService.clear_seats_for_adventurer(adventurer_id)
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            return {
                'adventurer': adv.to_dict(is_admin=True),
                'evicted_from': 'hot_spring',
            }, None

        # ── 场景 3：酒馆大堂座位 ──
        from plugins.adventurer_s1.models import AdventurerS1TavernSeat
        tavern_seat = AdventurerS1TavernSeat.query.filter_by(
            adventurer_id=adventurer_id,
        ).first()
        if tavern_seat:
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                AdventurerS1TavernSeatService,
            )
            AdventurerS1TavernSeatService.clear_seats_for_adventurer(adventurer_id)
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            return {
                'adventurer': adv.to_dict(is_admin=True),
                'evicted_from': 'tavern',
            }, None

        return None, '该冒险者不在任何建筑或座位中'

    @staticmethod
    def _get_admin_user_ids() -> List[int]:
        from models.user import User

        return [
            uid for (uid,) in (
                User.query.with_entities(User.id)
                .filter(User.is_admin == True, User.is_deleted == False)  # noqa: E712
                .all()
            )
        ]

    @staticmethod
    def _load_digest_state() -> dict:
        from models.system_config import SystemConfig

        row = SystemConfig.query.filter_by(config_key=_INACTIVE_DIGEST_CONFIG_KEY).first()
        if not row or not row.config_value:
            return {}
        try:
            return json.loads(row.config_value)
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _save_digest_state(state: dict) -> None:
        from models.system_config import SystemConfig

        payload = json.dumps(state, ensure_ascii=False)
        row = SystemConfig.query.filter_by(config_key=_INACTIVE_DIGEST_CONFIG_KEY).first()
        if row:
            row.config_value = payload
        else:
            db_manager.add(SystemConfig(
                config_key=_INACTIVE_DIGEST_CONFIG_KEY,
                config_value=payload,
            ))
        db_manager.commit()

    @staticmethod
    def notify_admins_inactive_digest() -> None:
        """定时任务：向所有管理员发送不活跃冒险者摘要邮件（按间隔去重）。"""
        interval_hours = int(
            current_app.config.get('ADVENTURER_S1_INACTIVE_ADMIN_DIGEST_INTERVAL_HOURS', 24)
        )
        now = _portable_utc_now()
        state = AdventurerS1AdminService._load_digest_state()
        last_sent_raw = state.get('last_sent_at')
        if last_sent_raw:
            try:
                last_sent = datetime.fromisoformat(last_sent_raw)
                if now - last_sent < timedelta(hours=interval_hours):
                    return
            except (TypeError, ValueError):
                pass

        data, err = AdventurerS1AdminService.list_inactive_adventurers(page=1, per_page=100)
        if err or not data:
            return
        inactive_count = int(data.get('inactive_count') or 0)
        if inactive_count < 1:
            return

        admin_ids = AdventurerS1AdminService._get_admin_user_ids()
        if not admin_ids:
            return

        hours = int(data.get('inactive_hours') or 24)
        lines = []
        for item in data.get('items') or []:
            owner = item.get('owner_nickname') or item.get('owner_username') or '无主'
            lines.append(
                f"· {item.get('name')} (ID:{item.get('id')}) — 玩家 {owner}，"
                f"末次操作 {item.get('last_owner_action_at') or item.get('inactive_since') or '未知'}"
            )
        if inactive_count > len(lines):
            lines.append(f'… 另有 {inactive_count - len(lines)} 名未列出')

        content = (
            f'深渊酒馆 S1 检测到 {inactive_count} 名冒险者已超过 {hours} 小时无动静'
            f'（以玩家末次登录或冒险者更新时间为准）。\n\n'
            + '\n'.join(lines)
            + '\n\n请前往管理后台「冒险者管理」查看详情，必要时执行封存清退以释放床位。'
        )

        try:
            from services.mailbox_service import MailboxService
            for admin_id in admin_ids:
                MailboxService.send_mail(
                    user_id=admin_id,
                    title='深渊酒馆 · 不活跃冒险者提醒',
                    content=content,
                    sender_id=None,
                    sender_name='深渊酒馆',
                    mail_type='adventurer_s1_inactive_digest',
                )
            AdventurerS1AdminService._save_digest_state({
                'last_sent_at': now.isoformat(),
                'inactive_count': inactive_count,
            })
            logger.info(
                'S1 不活跃提醒已发送给 %s 名管理员，共 %s 名不活跃冒险者',
                len(admin_ids), inactive_count,
            )
        except Exception:
            logger.exception('S1 不活跃管理员提醒发送失败')

    _ITEM_HOLDER_SEARCH_MAX = 1000

    @staticmethod
    def search_item_holders(
        search: str,
        page: int = 1,
        per_page: int = 30,
        match: str = 'contains',
    ) -> Tuple[Optional[dict], Optional[str]]:
        """按物品名搜索全服持有者（随身装备 + 仓库）。"""
        from models.user import User

        keyword = (search or '').strip()
        if not keyword:
            return None, '请输入物品名称'
        if len(keyword) < 2:
            return None, '搜索词至少 2 个字符'

        match_mode = (match or 'contains').strip().lower()
        if match_mode not in ('contains', 'exact'):
            return None, 'match 仅支持 contains 或 exact'

        def _name_filter(model):
            if match_mode == 'exact':
                return model.name == keyword
            return model.name.ilike(f'%{keyword}%')

        eq_rows = (
            db_manager.query(AdventurerS1Equipment, AdventurerS1, User)
            .join(AdventurerS1, AdventurerS1Equipment.adventurer_id == AdventurerS1.id)
            .outerjoin(User, AdventurerS1.owner_id == User.id)
            .filter(_name_filter(AdventurerS1Equipment))
            .order_by(AdventurerS1Equipment.id.desc())
            .limit(AdventurerS1AdminService._ITEM_HOLDER_SEARCH_MAX)
            .all()
        )
        wh_rows = (
            db_manager.query(AdventurerS1WarehouseItem, AdventurerS1, User)
            .join(AdventurerS1, AdventurerS1WarehouseItem.adventurer_id == AdventurerS1.id)
            .outerjoin(User, AdventurerS1.owner_id == User.id)
            .filter(_name_filter(AdventurerS1WarehouseItem))
            .order_by(AdventurerS1WarehouseItem.id.desc())
            .limit(AdventurerS1AdminService._ITEM_HOLDER_SEARCH_MAX)
            .all()
        )

        items: list[dict] = []

        def _append_row(record, location: str, adv: AdventurerS1, user: Optional[User]) -> None:
            items.append({
                'record_id': record.id,
                'location': location,
                'item_name': record.name,
                'item_type': record.type or '',
                'rarity': record.rarity or 'common',
                'quantity': int(record.quantity or 1),
                'uses_left': record.uses_left,
                'has_custom_desc': bool(getattr(record, 'custom_desc', None)),
                'adventurer_id': adv.id,
                'adventurer_name': adv.name,
                'adventurer_status': adv.status,
                'owner_id': adv.owner_id,
                'username': user.username if user else None,
                'nickname': user.nickname if user else None,
            })

        for eq, adv, user in eq_rows:
            _append_row(eq, 'equipment', adv, user)
        for wh, adv, user in wh_rows:
            _append_row(wh, 'warehouse', adv, user)

        items.sort(key=lambda row: (row['item_name'], row['adventurer_name'], row['location'], row['record_id']))

        total = len(items)
        truncated = (
            len(eq_rows) >= AdventurerS1AdminService._ITEM_HOLDER_SEARCH_MAX
            or len(wh_rows) >= AdventurerS1AdminService._ITEM_HOLDER_SEARCH_MAX
        )
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 30), 100))
        start = (page - 1) * per_page
        page_items = items[start:start + per_page]

        unique_adventurers = len({row['adventurer_id'] for row in items})
        unique_owners = len({row['owner_id'] for row in items if row['owner_id']})

        return {
            'search': keyword,
            'match': match_mode,
            'summary': {
                'total_records': total,
                'unique_adventurers': unique_adventurers,
                'unique_owners': unique_owners,
                'truncated': truncated,
            },
            'items': page_items,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page),
        }, None

    @staticmethod
    def _batch_user_brief_map(user_ids: set[int]) -> dict[int, dict]:
        if not user_ids:
            return {}
        from models.user import User

        rows = (
            User.query.with_entities(User.id, User.username, User.nickname)
            .filter(User.id.in_(user_ids), User.is_deleted == False)  # noqa: E712
            .all()
        )
        return {
            uid: {'user_id': uid, 'username': username, 'nickname': nickname or username}
            for uid, username, nickname in rows
        }

    @staticmethod
    def get_party_match_overview(
        party_status: str = 'forming',
        ticket_status: str = 'active',
        party_limit: int = 100,
        ticket_limit: int = 100,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """管理员只读：匹配中 + 集结中（未进本）一览。默认不含 exploring / 已进本小队。"""
        from plugins.adventurer_s1.models import AdventurerS1MatchTicket, AdventurerS1MatchTicketMember, AdventurerS1Party
        from sqlalchemy.orm import selectinload
        from plugins.adventurer_s1.services.adventurer_s1_config_service import format_s1_dungeon_level_desc
        from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import _estimate_queue_parties_ahead_map
        from plugins.adventurer_s1.services.adventurer_s1_party_service import get_departure_votes_by_party_ids
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService

        party_limit = max(1, min(int(party_limit or 100), 200))
        ticket_limit = max(1, min(int(ticket_limit or 100), 200))

        ticket_q = AdventurerS1MatchTicket.query
        ticket_status_norm = (ticket_status or 'active').strip().lower()
        if ticket_status_norm == 'active':
            ticket_q = ticket_q.filter(AdventurerS1MatchTicket.status.in_(['forming', 'queued', 'matched']))
        elif ticket_status_norm != 'all':
            ticket_q = ticket_q.filter(AdventurerS1MatchTicket.status == ticket_status_norm)

        tickets = (
            ticket_q.options(
                selectinload(AdventurerS1MatchTicket.members).selectinload(
                    AdventurerS1MatchTicketMember.adventurer,
                ),
                selectinload(AdventurerS1MatchTicket.matched_party),
            )
            .order_by(
                AdventurerS1MatchTicket.status.desc(),
                AdventurerS1MatchTicket.queued_at.asc().nullsfirst(),
                AdventurerS1MatchTicket.created_at.asc(),
                AdventurerS1MatchTicket.id.asc(),
            )
            .limit(ticket_limit)
            .all()
        )
        # 已进本（exploring 等）的 matched 报名不展示
        tickets = [
            t for t in tickets
            if t.status in ('forming', 'queued')
            or (
                t.status == 'matched'
                and (not t.matched_party or t.matched_party.status == 'forming')
            )
        ]

        party_q = AdventurerS1TavernService._apply_party_status_filter(
            AdventurerS1Party.query, party_status or 'forming',
        )
        party_rows, party_total = AdventurerS1TavernService._fetch_parties_page(party_q, 1, party_limit)

        special_names = set(AdventurerS1TavernService._special_dungeon_names())
        votes_map = get_departure_votes_by_party_ids([p.id for p in party_rows])

        owner_ids: set[int] = set()
        for ticket in tickets:
            if ticket.owner_id:
                owner_ids.add(ticket.owner_id)
            for member in ticket.members:
                adv = member.adventurer
                if adv and adv.owner_id:
                    owner_ids.add(adv.owner_id)
        for party in party_rows:
            for member in party.members:
                if member.owner_id:
                    owner_ids.add(member.owner_id)

        user_map = AdventurerS1AdminService._batch_user_brief_map(owner_ids)
        queued_ids = [t.id for t in tickets if t.status == 'queued']
        queue_ahead_map = _estimate_queue_parties_ahead_map(queued_ids) if queued_ids else {}

        ticket_items = []
        for ticket in tickets:
            owner = user_map.get(ticket.owner_id) if ticket.owner_id else None
            item = {
                'id': ticket.id,
                'status': ticket.status,
                'queue_type': ticket.queue_type,
                'access_mode': ticket.access_mode,
                'invite_code': ticket.invite_code,
                'preferred_dungeon': ticket.preferred_dungeon,
                'preferred_dungeons': ticket.preferred_dungeons or [],
                'preferred_dungeon_level_desc': format_s1_dungeon_level_desc(ticket.preferred_dungeon),
                'description': ticket.description,
                'match_error': ticket.match_error or '',
                'owner_id': ticket.owner_id,
                'owner_nickname': owner['nickname'] if owner else None,
                'created_at': to_utc_iso(ticket.created_at) if ticket.created_at else None,
                'queued_at': to_utc_iso(ticket.queued_at) if ticket.queued_at else None,
                'member_count': len(ticket.members),
                'members': [],
            }
            if ticket.status == 'queued':
                item['queue_parties_ahead'] = queue_ahead_map.get(ticket.id, 0)
            if ticket.matched_party_id:
                mp = ticket.matched_party
                item['matched_party_id'] = ticket.matched_party_id
                if mp:
                    item['matched_party_name'] = mp.name
                    item['matched_party_status'] = mp.status
                    item['matched_party_dungeon'] = mp.target_dungeon
            for member in ticket.members:
                adv = member.adventurer
                member_owner = user_map.get(adv.owner_id) if adv and adv.owner_id else None
                item['members'].append({
                    'adventurer_id': member.adventurer_id,
                    'name': adv.name if adv else None,
                    'level': adv.level if adv else None,
                    'status': adv.status if adv else None,
                    'owner_id': adv.owner_id if adv else None,
                    'owner_nickname': member_owner['nickname'] if member_owner else None,
                })
            ticket_items.append(item)

        party_items = []
        for party in party_rows:
            payload = party.to_dict(departure_voted_adventurer_ids=votes_map.get(party.id, []))
            payload['kind'] = 'special' if party.target_dungeon in special_names else 'normal'
            payload['members'] = []
            for member in party.members:
                member_owner = user_map.get(member.owner_id) if member.owner_id else None
                payload['members'].append({
                    'id': member.id,
                    'name': member.name,
                    'hp': member.hp,
                    'max_hp': member.max_hp,
                    'level': member.level,
                    'owner_id': member.owner_id,
                    'owner_nickname': member_owner['nickname'] if member_owner else None,
                })
            party_items.append(payload)

        summary = {
            'match_ticket_count': len(ticket_items),
            'queued_ticket_count': sum(1 for t in ticket_items if t['status'] == 'queued'),
            'forming_ticket_count': sum(1 for t in ticket_items if t['status'] == 'forming'),
            'matched_ticket_count': sum(1 for t in ticket_items if t['status'] == 'matched'),
            'party_count': party_total,
            'forming_party_count': sum(1 for p in party_items if p['status'] == 'forming'),
        }

        return {
            'summary': summary,
            'match_tickets': ticket_items,
            'parties': party_items,
            'party_total': party_total,
            'party_limit': party_limit,
        }, None
