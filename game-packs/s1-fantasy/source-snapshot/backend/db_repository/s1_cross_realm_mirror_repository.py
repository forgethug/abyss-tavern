"""Persistence helpers for the S1 one-shot cross-realm mirror ticket."""

from __future__ import annotations

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1RealmMembership,
    AdventurerS1Settings,
)

from db_repository.s1_realm_repository import S1RealmRepository


class S1CrossRealmMirrorRepository:
    """Keep ticket locking and source-adventurer reads out of the service."""

    @staticmethod
    def lock_owner_user(user_id: int):
        return S1RealmRepository.lock_user(int(user_id))

    @staticmethod
    def get_membership(user_id: int, *, for_update: bool = False):
        return S1RealmRepository.get_membership(
            int(user_id),
            for_update=for_update,
        )

    @staticmethod
    def get_ticket(setting_key: str, *, for_update: bool = False):
        query = AdventurerS1Settings.query.filter_by(key=str(setting_key))
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.first()

    @staticmethod
    def get_tickets(setting_keys: list[str]) -> list[AdventurerS1Settings]:
        return (
            AdventurerS1Settings.query
            .filter(AdventurerS1Settings.key.in_(setting_keys))
            .order_by(AdventurerS1Settings.key)
            .populate_existing()
            .with_for_update()
            .all()
        )

    @staticmethod
    def get_active_source_names(user_ids: list[int], *, realm_code: str) -> dict[int, str]:
        rows = (
            db.session.query(AdventurerS1.owner_id, AdventurerS1.name)
            .join(
                AdventurerS1RealmMembership,
                (AdventurerS1RealmMembership.user_id == AdventurerS1.owner_id)
                & (AdventurerS1RealmMembership.active_adventurer_id == AdventurerS1.id),
            )
            .filter(
                AdventurerS1.owner_id.in_(user_ids),
                AdventurerS1.realm_code == realm_code,
                AdventurerS1RealmMembership.realm_code == realm_code,
                AdventurerS1RealmMembership.slot_state == 'active',
                AdventurerS1.is_player_created.is_(True),
                ~AdventurerS1.status.in_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .all()
        )
        return {int(uid): str(name) for uid, name in rows}

    @staticmethod
    def add_ticket(ticket: AdventurerS1Settings) -> None:
        db.session.add(ticket)

    @staticmethod
    def get_active_source_adventurer(
        *,
        adventurer_id: int,
        owner_user_id: int,
        realm_code: str,
    ) -> AdventurerS1 | None:
        return AdventurerS1.query.filter(
            AdventurerS1.id == int(adventurer_id),
            AdventurerS1.owner_id == int(owner_user_id),
            AdventurerS1.realm_code == str(realm_code),
            AdventurerS1.is_player_created.is_(True),
            ~AdventurerS1.status.in_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        ).first()

    @staticmethod
    def get_active_source_name(
        *,
        adventurer_id: int,
        owner_user_id: int,
        realm_code: str,
    ) -> str | None:
        row = (
            db.session.query(AdventurerS1.name)
            .filter(
                AdventurerS1.id == int(adventurer_id),
                AdventurerS1.owner_id == int(owner_user_id),
                AdventurerS1.realm_code == str(realm_code),
                AdventurerS1.is_player_created.is_(True),
                ~AdventurerS1.status.in_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .first()
        )
        return str(row[0]) if row and row[0] else None
