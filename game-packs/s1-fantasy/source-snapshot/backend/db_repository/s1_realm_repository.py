"""Database query shapes and stable owner locks for S1 realm isolation."""

from __future__ import annotations

from sqlalchemy import func

from extensions import db
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1RealmMembership,
    AdventurerS1Reservation,
    AdventurerS1Settings,
)


class S1RealmRepository:
    """Keep S1 global active-slot and owner queries in one repository."""

    @staticmethod
    def lock_user(user_id: int) -> User | None:
        """Lock the stable owner aggregate before membership/roster writes."""
        return (
            db.session.query(User)
            .filter(User.id == int(user_id))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )

    @staticmethod
    def lock_users(user_ids: list[int]) -> list[User]:
        """Lock owner aggregates once, in stable order, for global maintenance."""
        normalized = sorted({int(user_id) for user_id in user_ids})
        if not normalized:
            return []
        return (
            db.session.query(User)
            .filter(User.id.in_(normalized))
            .order_by(User.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )

    @staticmethod
    def get_membership(
        user_id: int,
        *,
        for_update: bool = False,
    ) -> AdventurerS1RealmMembership | None:
        query = db.session.query(AdventurerS1RealmMembership).filter(
            AdventurerS1RealmMembership.user_id == int(user_id),
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.one_or_none()

    @staticmethod
    def get_membership_realm_code(user_id: int) -> str | None:
        """Project only the current global living-slot realm for hot reads."""
        row = (
            db.session.query(AdventurerS1RealmMembership.realm_code)
            .filter(AdventurerS1RealmMembership.user_id == int(user_id))
            .one_or_none()
        )
        return None if row is None else str(row[0])

    @staticmethod
    def add_membership(membership: AdventurerS1RealmMembership) -> None:
        db.session.add(membership)
        db.session.flush()

    @staticmethod
    def delete_membership(membership: AdventurerS1RealmMembership) -> None:
        db.session.delete(membership)
        db.session.flush()

    @staticmethod
    def list_memberships_for_realm(
        realm_code: str,
        *,
        for_update: bool = False,
    ) -> list[AdventurerS1RealmMembership]:
        query = (
            db.session.query(AdventurerS1RealmMembership)
            .filter(AdventurerS1RealmMembership.realm_code == str(realm_code))
            .order_by(AdventurerS1RealmMembership.user_id.asc())
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.all()

    @staticmethod
    def list_memberships_for_users(
        user_ids: list[int],
        *,
        for_update: bool = False,
    ) -> list[AdventurerS1RealmMembership]:
        normalized = sorted({int(user_id) for user_id in user_ids})
        if not normalized:
            return []
        query = (
            db.session.query(AdventurerS1RealmMembership)
            .filter(AdventurerS1RealmMembership.user_id.in_(normalized))
            .order_by(AdventurerS1RealmMembership.user_id.asc())
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.all()

    @staticmethod
    def get_region_2_admission_setting(
        *,
        for_update: bool = False,
    ) -> AdventurerS1Settings | None:
        """Read or lock the persisted admission switch used by realm claims."""
        query = db.session.query(AdventurerS1Settings).filter(
            AdventurerS1Settings.key == 'region_2_enabled',
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.one_or_none()

    @staticmethod
    def list_history_realm_evidence(user_id: int) -> list[tuple[str, str]]:
        """Return only currently living local adventurer realm evidence."""
        uid = int(user_id)
        rows = (
            db.session.query(
                func.coalesce(AdventurerS1.realm_code, 'region_1').label(
                    'realm_code'
                ),
            )
            .filter(
                AdventurerS1.owner_id == uid,
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .distinct()
            .all()
        )
        return [('adventurer', str(realm_code)) for (realm_code,) in rows]

    @staticmethod
    def list_active_player_adventurer_slots() -> list[tuple[int, int, str]]:
        """Materialize the local Runtime inventory used for central reconcile."""
        rows = (
            db.session.query(
                AdventurerS1.owner_id,
                AdventurerS1.id,
                AdventurerS1.realm_code,
            )
            .filter(
                AdventurerS1.owner_id.isnot(None),
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(AdventurerS1.owner_id.asc(), AdventurerS1.id.asc())
            .all()
        )
        return [
            (int(user_id), int(adventurer_id), str(realm_code))
            for user_id, adventurer_id, realm_code in rows
        ]

    @staticmethod
    def count_active_player_adventurers(
        user_id: int,
        *,
        exclude_adventurer_id: int | None = None,
    ) -> int:
        query = db.session.query(func.count(AdventurerS1.id)).filter(
            AdventurerS1.owner_id == int(user_id),
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )
        if exclude_adventurer_id is not None:
            query = query.filter(AdventurerS1.id != int(exclude_adventurer_id))
        return int(query.scalar() or 0)

    @staticmethod
    def find_archived_player_adventurer(user_id: int) -> AdventurerS1 | None:
        return (
            db.session.query(AdventurerS1)
            .filter(
                AdventurerS1.owner_id == int(user_id),
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.status == 'archived',
            )
            .order_by(AdventurerS1.id.asc())
            .first()
        )

    @staticmethod
    def find_pending_reservation(
        user_id: int,
        *,
        exclude_reservation_id: int | None = None,
    ) -> AdventurerS1Reservation | None:
        query = db.session.query(AdventurerS1Reservation).filter(
            AdventurerS1Reservation.user_id == int(user_id),
            AdventurerS1Reservation.status == 'pending',
        )
        if exclude_reservation_id is not None:
            query = query.filter(
                AdventurerS1Reservation.id != int(exclude_reservation_id),
            )
        return query.order_by(AdventurerS1Reservation.id.asc()).first()

    @staticmethod
    def lock_reservation(reservation_id: int) -> AdventurerS1Reservation | None:
        return (
            db.session.query(AdventurerS1Reservation)
            .filter(AdventurerS1Reservation.id == int(reservation_id))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )

    @staticmethod
    def lock_adventurer(adventurer_id: int) -> AdventurerS1 | None:
        return (
            db.session.query(AdventurerS1)
            .filter(AdventurerS1.id == int(adventurer_id))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
