"""Campaign task waiting and cooldown reads; caller owns each short transaction."""
from sqlalchemy import func, or_, true
from sqlalchemy.orm import noload

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1 as Adventurer, AdventurerS1ExpeditionLog as Log,
    AdventurerS1MatchTicket as Ticket, AdventurerS1MatchTicketMember as Member,
    AdventurerS1Party as Party, AdventurerS1PartyMember as PartyMember,
)


class S1CampaignTaskAdmissionRepository:
    @staticmethod
    def expired_candidates(cutoff, *, limit):
        departed = db_manager.query(Log.id).filter(Log.party_id == Party.id).exists()
        waiting_party = db_manager.query(Party.id).filter(
            Party.id == Ticket.matched_party_id, Party.status == 'forming', ~departed,
        ).exists()
        return db_manager.query(Ticket.id, Ticket.matched_party_id, Ticket.status).filter(
            Ticket.campaign_dispatch_id.isnot(None), Ticket.created_at <= cutoff,
            or_(Ticket.status.in_(('forming', 'queued')),
                (Ticket.status == 'matched') & waiting_party),
        ).order_by(Ticket.created_at, Ticket.id).limit(limit).all()

    @staticmethod
    def lock_ticket(ticket_id):
        return db_manager.query(Ticket).options(noload('*')).populate_existing().filter_by(
            id=ticket_id,
        ).with_for_update().first()

    @staticmethod
    def lock_ticket_adventurers(ticket_id):
        ids = db_manager.query(Member.adventurer_id).filter(Member.ticket_id == ticket_id)
        return db_manager.query(Adventurer).options(noload('*')).populate_existing().filter(
            Adventurer.id.in_(ids),
        ).order_by(Adventurer.id).with_for_update().all()

    @staticmethod
    def lock_waiting_party(party_id):
        ids = db_manager.query(PartyMember.adventurer_id).filter(PartyMember.party_id == party_id)
        # 与发车/成团后离队一致：adventurer -> party -> matched tickets。
        db_manager.query(Adventurer).options(noload('*')).populate_existing().filter(
            Adventurer.id.in_(ids),
        ).order_by(Adventurer.id).with_for_update().all()
        return db_manager.query(Party).options(noload('*')).populate_existing().filter_by(
            id=party_id,
        ).with_for_update().first()

    @staticmethod
    def has_departure_log(party_id):
        return db_manager.query(Log.id).filter(Log.party_id == party_id).first() is not None

    @staticmethod
    def campaign_tickets_for_party(party_id):
        return db_manager.query(Ticket).options(noload('*')).populate_existing().filter(
            Ticket.matched_party_id == party_id, Ticket.status == 'matched',
            Ticket.campaign_dispatch_id.isnot(None),
        ).order_by(Ticket.id).with_for_update().all()

    @staticmethod
    def _belongs_to(user_id):
        owned_ticket_ids = db_manager.query(Ticket.id).filter(
            Ticket.owner_id == user_id,
        ).correlate(None)
        member_ticket_ids = db_manager.query(Member.ticket_id).join(
            Adventurer, Adventurer.id == Member.adventurer_id,
        ).filter(Adventurer.owner_id == user_id)
        # 先以 owner/member 索引形成个人 ticket 集合，避免全表历史报名上的 OR 扫描。
        return Ticket.id.in_(owned_ticket_ids.union(member_ticket_ids))

    @classmethod
    def _cooldown_query(cls, user_id, now):
        # 先按索引排除已过期凭据，再核对账号，改换角色也不会绕过冷却。
        return db_manager.query(func.max(Ticket.campaign_task_cooldown_until).label('cooldown_until')).filter(
            Ticket.status == 'cancelled', Ticket.campaign_task_cooldown_until > now,
            cls._belongs_to(user_id),
        )

    @classmethod
    def cooldown_until(cls, user_id, now):
        return cls._cooldown_query(user_id, now).scalar()

    @classmethod
    def public_state(cls, user_id, now):
        # 只投影玩家文案和时间，不加载旧队伍、资产或后台活动身份。
        notice = db_manager.query(Ticket.match_error, Ticket.cancelled_at).filter(
            Ticket.status == 'cancelled', Ticket.match_error != '', Ticket.cancelled_at.isnot(None),
            or_(Ticket.campaign_dispatch_id.isnot(None),
                Ticket.campaign_task_cooldown_until.isnot(None)), cls._belongs_to(user_id),
        ).order_by(Ticket.cancelled_at.desc(), Ticket.id.desc()).limit(1).subquery()
        cooldown = cls._cooldown_query(user_id, now).subquery()
        # 聚合即使没有冷却也产出一行，LEFT JOIN 保证没有历史提示时仍返回公开规则。
        return db_manager.query(cooldown.c.cooldown_until, notice.c.match_error, notice.c.cancelled_at).select_from(
            cooldown,
        ).outerjoin(notice, true()).first()
