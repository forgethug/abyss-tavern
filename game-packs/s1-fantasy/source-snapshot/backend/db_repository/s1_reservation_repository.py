"""Scalar reservation/adventurer detection evidence and conditional priority writes."""

from sqlalchemy import func

from db_repository import db_manager
from models.user import User
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Reservation


class S1ReservationRepository:
    @staticmethod
    def list_alt_candidates(realm_code):
        return db_manager.query(
            AdventurerS1Reservation.id,
            AdventurerS1Reservation.user_id,
            AdventurerS1Reservation.name,
            AdventurerS1Reservation.status,
            AdventurerS1Reservation.priority,
        ).filter(
            AdventurerS1Reservation.realm_code == realm_code,
            AdventurerS1Reservation.status.in_(('pending', 'drawn')),
        ).order_by(
            AdventurerS1Reservation.created_at.desc(),
            AdventurerS1Reservation.id.desc(),
        ).all()

    @staticmethod
    def list_adventurer_alt_candidates(realm_code):
        return db_manager.query(
            AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.name,
            AdventurerS1.status, AdventurerS1.level,
        ).filter(
            AdventurerS1.realm_code == realm_code,
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.owner_id.isnot(None),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        ).order_by(AdventurerS1.id.asc()).all()

    @staticmethod
    def list_latest_reservation_statuses(user_ids, realm_code):
        if not user_ids:
            return []
        ranked = db_manager.query(
            AdventurerS1Reservation.user_id,
            AdventurerS1Reservation.status,
            func.row_number().over(
                partition_by=AdventurerS1Reservation.user_id,
                order_by=(
                    AdventurerS1Reservation.created_at.desc(),
                    AdventurerS1Reservation.id.desc(),
                ),
            ).label('row_rank'),
        ).filter(
            AdventurerS1Reservation.realm_code == realm_code,
            AdventurerS1Reservation.user_id.in_(user_ids),
        ).subquery()
        return db_manager.query(ranked.c.user_id, ranked.c.status).filter(
            ranked.c.row_rank == 1,
        ).all()

    @staticmethod
    def list_ip_user_profiles(user_ids):
        return [dict(row._mapping) for row in db_manager.query(
            User.id, User.username, User.nickname, User.is_frozen,
            User.register_ip, User.last_login_ip,
        ).filter(User.id.in_(user_ids), User.is_deleted.is_(False)).all()]

    @staticmethod
    def has_elevated_reservation(realm_code):
        return db_manager.query(AdventurerS1Reservation.id).filter(
            AdventurerS1Reservation.realm_code == realm_code,
            AdventurerS1Reservation.status == 'pending',
            AdventurerS1Reservation.priority == 'elevated',
        ).first() is not None

    @staticmethod
    def lower_elevated_priorities(reservation_ids, realm_code):
        if not reservation_ids:
            return 0
        return db_manager.query(AdventurerS1Reservation).filter(
            AdventurerS1Reservation.id.in_(reservation_ids),
            AdventurerS1Reservation.realm_code == realm_code,
            AdventurerS1Reservation.status == 'pending',
            AdventurerS1Reservation.priority == 'elevated',
        ).update({
            AdventurerS1Reservation.priority: 'low',
            AdventurerS1Reservation.priority_boost_until: None,
        }, synchronize_session=False)
