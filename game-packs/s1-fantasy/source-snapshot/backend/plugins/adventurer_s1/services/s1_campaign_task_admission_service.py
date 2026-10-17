"""Cancel undeparted task squads and enforce the account's acceptance cooldown."""
from runtime_core.time import utc_now as _portable_utc_now
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import current_app

from db_repository import db_manager
from db_repository.s1_campaign_task_admission_repository import (
    S1CampaignTaskAdmissionRepository as Repo,
)
from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
    AdventurerS1StateTransitionCoordinator,
)
from utils.time_utils import to_utc_iso


def task_policy():
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    def minutes(suffix):
        key = 'ADVENTURER_S1_CAMPAIGN_TASK_' + suffix
        return max(1, int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])))

    return {
        'departure_timeout_minutes': minutes('DEPARTURE_TIMEOUT_MINUTES'),
        'timeout_cooldown_minutes': minutes('TIMEOUT_COOLDOWN_MINUTES'),
    }


def task_departure_deadline(ticket):
    if not ticket.campaign_dispatch_id or not ticket.created_at:
        return None
    return ticket.created_at + timedelta(minutes=task_policy()['departure_timeout_minutes'])


def timeout_notice():
    policy = task_policy()
    return (f"接取任务后 {policy['departure_timeout_minutes']} 分钟仍未出发，"
            f"小队已解散，队员已恢复休息；{policy['timeout_cooldown_minutes']} 分钟内"
            '不能再次接取或加入活动任务，普通探险不受影响。')


ADMISSION_CANCEL_NOTICE = '本次活动入口已更新或关闭，小队已解散，队员已恢复休息，请换接当前可用地图。'


@contextmanager
def notify_task_cancellations_after_commit():
    """Place before the SQL transaction context so invalidation runs after commit."""
    changed = [False]
    try:
        yield changed
    finally:
        # 部分批次已提交后下一队失败，也必须广播已经发生的变更。
        if changed[0]:
            from .adventurer_s1_matchmaking_service import _emit_matchmaking_changed
            _emit_matchmaking_changed('campaign_task_cancelled')


class S1CampaignTaskAdmissionService:
    @staticmethod
    def acceptance_error(user_id, *, now=None):
        until = Repo.cooldown_until(user_id, now or _portable_utc_now())
        if until:
            # 时间另在公开状态中提供，避免把 UTC 时间当成本地时间写入玩家文案。
            return '此前任务因未及时出发已取消，接任务冷却尚未结束；可先进行普通探险。'
        return None

    @staticmethod
    def public_state(user_id):
        state = Repo.public_state(user_id, _portable_utc_now())
        until = state.cooldown_until
        return {
            'campaign_task_cooldown_until': to_utc_iso(until) if until else None,
            'campaign_task_notice': ({'message': state.match_error,
                                      'cancelled_at': to_utc_iso(state.cancelled_at)}
                                     if state.match_error else None),
            'campaign_task_policy': task_policy(),
        }

    @staticmethod
    def cancel_expired_locked_ticket(ticket, *, now):
        """Caller owns ticket lock and transaction; matched parties use adv -> party."""
        deadline = task_departure_deadline(ticket)
        if ticket.status not in ('forming', 'queued') or not deadline or deadline > now:
            return False
        advs = Repo.lock_ticket_adventurers(ticket.id)
        for adv in advs:
            if adv.status == 'in_party':
                AdventurerS1StateTransitionCoordinator.enter_rest(adv)
        ticket.status = 'cancelled'
        ticket.match_error = timeout_notice()
        ticket.queued_at = None
        ticket.cancelled_at = now
        ticket.campaign_task_cooldown_until = now + timedelta(
            minutes=task_policy()['timeout_cooldown_minutes'])
        from .adventurer_s1_matchmaking_service import _cancel_pending_invites
        _cancel_pending_invites(ticket_id=ticket.id)
        return True

    @staticmethod
    def cancel_locked_party(party, *, now, expired_only=True):
        """Caller holds all member locks then party lock. Never cancel a departed log."""
        if not party or party.status != 'forming' or Repo.has_departure_log(party.id):
            return False
        if expired_only:
            tickets = Repo.campaign_tickets_for_party(party.id)
            if not any((deadline := task_departure_deadline(t)) and deadline <= now for t in tickets):
                return False
        from .adventurer_s1_party_service import finalize_party_disband
        finalize_party_disband(
            party.id,
            match_error=timeout_notice() if expired_only else ADMISSION_CANCEL_NOTICE,
            campaign_task_cooldown_until=(now + timedelta(
                minutes=task_policy()['timeout_cooldown_minutes']) if expired_only else None),
            cancelled_at=now,
        )
        return True

    @staticmethod
    def cancel_closed_activity_party(party_id):
        """Closed-map fast rejection must also recheck under the departure locks."""
        from .s1_campaign_world_service import campaign_world
        from .s1_world_location_state_service import S1WorldLocationStateService
        notice = None
        with notify_task_cancellations_after_commit() as changed, db_manager.transaction():
            party = Repo.lock_waiting_party(party_id)
            if (not party or party.status != 'forming' or Repo.has_departure_log(party_id)
                    or not campaign_world(party.target_dungeon)):
                return None
            now = _portable_utc_now()
            if S1CampaignTaskAdmissionService.cancel_locked_party(party, now=now):
                changed[0] = True
                notice = timeout_notice()
            elif not S1WorldLocationStateService.lock_dungeon_dispatchable(party.target_dungeon):
                changed[0] = S1CampaignTaskAdmissionService.cancel_locked_party(
                    party, now=now, expired_only=False)
                if changed[0]:
                    notice = ADMISSION_CANCEL_NOTICE
        return notice

    @staticmethod
    def expire_undeparted_tasks():
        """Independent scheduler job: bounded selection, one short transaction per squad."""
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin
        now = _portable_utc_now()
        cutoff = now - timedelta(minutes=task_policy()['departure_timeout_minutes'])
        key = 'ADVENTURER_S1_CAMPAIGN_TASK_EXPIRY_BATCH_SIZE'
        limit = max(1, int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])))
        with db_manager.transaction():
            candidates = [(r.id, r.matched_party_id if r.status == 'matched' else None)
                          for r in Repo.expired_candidates(cutoff, limit=limit)]
        changed = 0
        handled_parties = set()
        with notify_task_cancellations_after_commit() as invalidation:
            for ticket_id, party_id in candidates:
                try:
                    if party_id:
                        if party_id in handled_parties:
                            continue
                        handled_parties.add(party_id)
                        with db_manager.transaction():
                            party = Repo.lock_waiting_party(party_id)
                            cancelled = S1CampaignTaskAdmissionService.cancel_locked_party(party, now=now)
                    else:
                        with db_manager.transaction():
                            ticket = Repo.lock_ticket(ticket_id)
                            cancelled = bool(ticket and S1CampaignTaskAdmissionService.cancel_expired_locked_ticket(
                                ticket, now=now))
                except Exception:
                    current_app.logger.exception('S1 活动任务超时清理失败 ticket=%s party=%s', ticket_id, party_id)
                    continue
                if cancelled:
                    changed += 1
                    invalidation[0] = True
        if changed:
            current_app.logger.info('S1 活动任务未出发超时自动解散 count=%s', changed)
        return changed
