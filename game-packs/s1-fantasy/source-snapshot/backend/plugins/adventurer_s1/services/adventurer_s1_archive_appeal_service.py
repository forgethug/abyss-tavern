"""封存清退玩家的解封申请：提交 → 管理员审核 → 入预约队列 → 抽中后解封原冒险者。"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
from datetime import datetime
from typing import Optional, Tuple

from db_repository import db_manager
from db_repository.s1_realm_repository import S1RealmRepository
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ArchiveAppeal,
    AdventurerS1Item,
    AdventurerS1Reservation,
    AdventurerS1SkillTemplate,
)
from utils.pagination import build_paginated_response
from plugins.adventurer_s1.services.s1_realm_service import S1RealmService

_APPEAL_REASON_MIN = 10
_APPEAL_REASON_MAX = 500


class AdventurerS1ArchiveAppealService:
    @staticmethod
    def _get_archived_adventurer(user_id: int, adventurer_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '无权操作该冒险者'
        if adv.status != 'archived':
            return None, '只有已封存清退的冒险者才能申请解封'
        return adv, None

    @staticmethod
    def _has_blocking_queue(user_id: int, *, exclude_appeal_id: Optional[int] = None) -> Optional[str]:
        pending_res = AdventurerS1Reservation.query.filter_by(
            user_id=user_id, status='pending',
        ).first()
        if pending_res:
            return '你已在解封/预约排队中，请等待抽取结果'
        q = AdventurerS1ArchiveAppeal.query.filter_by(
            user_id=user_id, status='pending',
        )
        if exclude_appeal_id is not None:
            q = q.filter(AdventurerS1ArchiveAppeal.id != exclude_appeal_id)
        pending_appeal = q.first()
        if pending_appeal:
            return '你已有一个解封申请待审核，请耐心等待'
        return None

    @staticmethod
    def _placeholder_creation_ids() -> Tuple[Optional[int], Optional[list], Optional[str]]:
        tpl = AdventurerS1SkillTemplate.query.filter_by(is_active=True).order_by(
            AdventurerS1SkillTemplate.id,
        ).first()
        if not tpl:
            return None, None, '系统尚未配置可用初始技能'
        items = AdventurerS1Item.query.filter_by(is_starter=True).order_by(
            AdventurerS1Item.id,
        ).limit(2).all()
        if len(items) < 2:
            return None, None, '系统尚未配置足够的初始装备占位'
        return tpl.id, [items[0].id, items[1].id], None

    @staticmethod
    def submit_appeal(
        user_id: int,
        adventurer_id: int,
        reason: str,
    ) -> Tuple[Optional[dict], Optional[str]]:
        adv, err = AdventurerS1ArchiveAppealService._get_archived_adventurer(user_id, adventurer_id)
        if err:
            return None, err

        reason = (reason or '').strip()
        if len(reason) < _APPEAL_REASON_MIN:
            return None, f'请填写至少 {_APPEAL_REASON_MIN} 字的解封理由'
        if len(reason) > _APPEAL_REASON_MAX:
            return None, f'解封理由过长（最多 {_APPEAL_REASON_MAX} 字）'

        block_err = AdventurerS1ArchiveAppealService._has_blocking_queue(user_id)
        if block_err:
            return None, block_err

        try:
            with db_manager.transaction():
                appeal = AdventurerS1ArchiveAppeal(
                    user_id=user_id,
                    adventurer_id=adv.id,
                    reason=reason,
                    status='pending',
                )
                ok, add_err = db_manager.add(appeal)
                if not ok:
                    raise RuntimeError(add_err)
                new_id = appeal.id
        except Exception as e:
            return None, player_operation_failure(e, '提交失败')

        appeal = AdventurerS1ArchiveAppeal.query.session.get(AdventurerS1ArchiveAppeal, new_id)
        return appeal.to_player_dict(), None

    @staticmethod
    def get_my_appeal(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """返回玩家最近一条有效解封申请（pending / approved 且排队中 / rejected 可重提）。"""
        appeal = (
            AdventurerS1ArchiveAppeal.query
            .filter(
                AdventurerS1ArchiveAppeal.user_id == user_id,
                AdventurerS1ArchiveAppeal.status.in_(('pending', 'approved', 'rejected')),
            )
            .order_by(AdventurerS1ArchiveAppeal.created_at.desc())
            .first()
        )
        if not appeal:
            return None, None

        if appeal.status == 'approved' and appeal.reservation_id:
            res = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, appeal.reservation_id)
            if not res or res.status != 'pending':
                return None, None

        return appeal.to_player_dict(), None

    @staticmethod
    def list_admin(
        page: int,
        per_page: int,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        q = AdventurerS1ArchiveAppeal.query
        if status and status in ('pending', 'approved', 'rejected', 'cancelled'):
            q = q.filter(AdventurerS1ArchiveAppeal.status == status)
        if search:
            search = search.strip()
            from extensions import db
            from models.user import User

            if search.isdigit():
                q = q.filter(AdventurerS1ArchiveAppeal.user_id == int(search))
            else:
                like = f'%{search}%'
                q = (
                    q.join(AdventurerS1, AdventurerS1ArchiveAppeal.adventurer_id == AdventurerS1.id)
                    .join(User, AdventurerS1ArchiveAppeal.user_id == User.id)
                    .filter(
                        db.or_(
                            AdventurerS1.name.ilike(like),
                            User.username.ilike(like),
                            User.nickname.ilike(like),
                        )
                    )
                )
        q = q.order_by(AdventurerS1ArchiveAppeal.created_at.desc())
        total = q.count()
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        items = [r.to_dict(include_admin=True) for r in rows]
        return build_paginated_response(items, total, page, per_page), None

    @staticmethod
    def approve(appeal_id: int, admin_user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        appeal = AdventurerS1ArchiveAppeal.query.session.get(AdventurerS1ArchiveAppeal, appeal_id)
        if not appeal:
            return None, '解封申请不存在'
        if appeal.status != 'pending':
            return None, f'该申请状态为 {appeal.status}，无法审核通过'

        adv, err = AdventurerS1ArchiveAppealService._get_archived_adventurer(
            appeal.user_id, appeal.adventurer_id,
        )
        if err:
            return None, err

        block_err = AdventurerS1ArchiveAppealService._has_blocking_queue(
            appeal.user_id, exclude_appeal_id=appeal_id,
        )
        if block_err:
            return None, block_err

        skill_id, item_ids, placeholder_err = AdventurerS1ArchiveAppealService._placeholder_creation_ids()
        if placeholder_err:
            return None, placeholder_err

        now = _portable_utc_now()
        reservation_id = None
        try:
            with db_manager.transaction():
                if S1RealmRepository.lock_user(appeal.user_id) is None:
                    raise RuntimeError('玩家不存在')
                appeal = (
                    AdventurerS1ArchiveAppeal.query
                    .filter_by(id=int(appeal_id))
                    .populate_existing()
                    .with_for_update()
                    .one()
                )
                if appeal.status != 'pending':
                    raise RuntimeError('解封申请已不在待审核状态')
                adv = S1RealmRepository.lock_adventurer(appeal.adventurer_id)
                if not adv or adv.owner_id != appeal.user_id or adv.status != 'archived':
                    raise RuntimeError('封存冒险者状态或归属已变化')
                S1RealmService.require_free_active_slot(appeal.user_id)
                if S1RealmRepository.find_pending_reservation(appeal.user_id):
                    raise RuntimeError('该玩家已有其他等待中的预约，无法批准解封')
                reservation = AdventurerS1Reservation(
                    user_id=appeal.user_id,
                    name=adv.name,
                    gender=adv.gender,
                    personality=adv.personality,
                    skill_template_id=skill_id,
                    item_ids=item_ids,
                    restore_adventurer_id=adv.id,
                    priority='normal',
                    status='pending',
                    realm_code=S1RealmService._validate_realm_code(
                        adv.realm_code or S1RealmService.current_runtime_realm()
                    ),
                )
                ok, add_err = db_manager.add(reservation)
                if not ok:
                    raise RuntimeError(add_err)
                reservation_id = reservation.id

                appeal.status = 'approved'
                appeal.reservation_id = reservation_id
                appeal.reviewed_by_user_id = admin_user_id
                appeal.reviewed_at = now
                appeal.reject_reason = None
        except Exception as e:
            return None, f'审核通过失败：{e}'

        reservation = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, reservation_id)
        return {
            'appeal': appeal.to_dict(include_admin=True),
            'reservation': reservation.to_dict() if reservation else None,
        }, None

    @staticmethod
    def reject(
        appeal_id: int,
        admin_user_id: int,
        reason: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        appeal = AdventurerS1ArchiveAppeal.query.session.get(AdventurerS1ArchiveAppeal, appeal_id)
        if not appeal:
            return None, '解封申请不存在'
        if appeal.status != 'pending':
            return None, f'该申请状态为 {appeal.status}，无法拒绝'

        try:
            with db_manager.transaction():
                appeal = (
                    AdventurerS1ArchiveAppeal.query
                    .filter_by(id=int(appeal_id))
                    .populate_existing()
                    .with_for_update()
                    .one_or_none()
                )
                if appeal is None or appeal.status != 'pending':
                    raise RuntimeError('解封申请状态已经变化，请刷新页面')
                appeal.status = 'rejected'
                appeal.reject_reason = (reason or '').strip() or '管理员拒绝解封申请'
                appeal.reviewed_by_user_id = admin_user_id
                appeal.reviewed_at = _portable_utc_now()
        except Exception as e:
            return None, f'操作失败：{e}'

        return appeal.to_dict(include_admin=True), None

    @staticmethod
    def mark_cancelled_for_reservation(reservation_id: int) -> None:
        """玩家取消解封排队时，同步关闭关联申请以便重新提交。"""
        appeal = AdventurerS1ArchiveAppeal.query.filter_by(
            reservation_id=reservation_id,
            status='approved',
        ).first()
        if not appeal:
            return
        appeal.status = 'cancelled'
        appeal.reservation_id = None

    @staticmethod
    def restore_approved_for_reservation(r: AdventurerS1Reservation) -> Tuple[bool, Optional[str]]:
        """管理员恢复误拒绝的解封排队预约时，重新关联已取消的解封申请。"""
        if not r.restore_adventurer_id:
            return True, None

        pending_appeal = AdventurerS1ArchiveAppeal.query.filter_by(
            user_id=r.user_id, status='pending',
        ).first()
        if pending_appeal:
            return False, '该玩家已有待审核的解封申请，无法恢复'

        appeal = (
            AdventurerS1ArchiveAppeal.query
            .filter_by(
                user_id=r.user_id,
                adventurer_id=r.restore_adventurer_id,
                status='cancelled',
            )
            .order_by(AdventurerS1ArchiveAppeal.updated_at.desc())
            .first()
        )
        if not appeal:
            return False, '未找到可恢复的解封申请记录'

        appeal.status = 'approved'
        appeal.reservation_id = r.id
        return True, None
