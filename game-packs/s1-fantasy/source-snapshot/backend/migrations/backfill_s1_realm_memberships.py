"""Migrate legacy permanent realm rows into current living-role slots."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from typing import Any

from sqlalchemy import inspect, text

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1RealmMembership,
)


_MIGRATION_LOCK_KEY = 'gupiao:s1_active_realm_slots:v2'
_TABLE = 'adventurer_s1_realm_memberships'
_VALID_REALMS = {'region_1', 'region_2'}


def _ensure_columns(connection) -> None:
    """Add current columns before ORM reads on an existing production table."""
    columns = {column['name'] for column in inspect(connection).get_columns(_TABLE)}
    additions = {
        'slot_state': "VARCHAR(20) NOT NULL DEFAULT 'active'",
        'claim_token': 'VARCHAR(64)',
        'claim_reservation_id': 'INTEGER',
        'active_adventurer_id': 'INTEGER',
        'reconcile_absent_since': 'TIMESTAMP',
        'updated_at': 'TIMESTAMP',
    }
    for name, ddl in additions.items():
        if name not in columns:
            connection.execute(text(f'ALTER TABLE {_TABLE} ADD COLUMN {name} {ddl}'))
    connection.execute(text(
        f'CREATE UNIQUE INDEX IF NOT EXISTS uq_advs1_active_slot_claim_token '
        f'ON {_TABLE} (claim_token)'
    ))
    connection.execute(text(
        f'UPDATE {_TABLE} SET slot_state = :state '
        f'WHERE slot_state IS NULL OR slot_state NOT IN (:pending, :active)'
    ), {'state': 'active', 'pending': 'pending', 'active': 'active'})


def migrate(*, engine=None, now: datetime | None = None) -> dict[str, Any]:
    """Idempotently keep slots only for currently living local adventurers.

    Legacy region-1 reservation-only rows have no ``active_adventurer_id`` and
    are removed because the central database can prove that no local living
    role exists.  An equally shaped legacy region-2 row is preserved as a
    pending reconciliation claim: only the isolated Runtime's full inventory
    can safely distinguish an old queue row from a living remote role.
    """
    target_engine = engine or db.engine
    migrated_at = now or _portable_utc_now()
    with target_engine.begin() as connection:
        if target_engine.dialect.name == 'postgresql':
            connection.execute(text(
                'SELECT pg_advisory_xact_lock(hashtext(:key))'
            ), {'key': _MIGRATION_LOCK_KEY})
        _ensure_columns(connection)

        active_rows = connection.execute(text(
            'SELECT owner_id, id, COALESCE(realm_code, :realm_1) AS realm_code '
            'FROM adventurer_s1 '
            'WHERE owner_id IS NOT NULL AND is_player_created = :is_player '
            "AND status NOT IN ('dead', 'archived') "
            'ORDER BY owner_id, id'
        ), {'realm_1': 'region_1', 'is_player': True}).mappings().all()
        active_by_user: dict[int, tuple[int, str]] = {}
        duplicate_users: set[int] = set()
        for row in active_rows:
            user_id = int(row['owner_id'])
            adventurer_id = int(row['id'])
            realm_code = str(row['realm_code'])
            if realm_code not in _VALID_REALMS:
                raise RuntimeError('S1 active-slot migration found invalid realm data')
            if user_id in active_by_user:
                duplicate_users.add(user_id)
            active_by_user[user_id] = (adventurer_id, realm_code)
        if duplicate_users:
            raise RuntimeError(
                'S1 active-slot migration found multiple living roles for one account'
            )

        existing_rows = connection.execute(text(
            f'SELECT user_id, realm_code, slot_state, claim_token, '
            f'claim_reservation_id, active_adventurer_id, '
            f'reconcile_absent_since '
            f'FROM {_TABLE}'
        )).mappings().all()
        existing_by_user = {int(row['user_id']): row for row in existing_rows}
        inserted = 0
        updated = 0
        conflicts = 0
        for user_id, (adventurer_id, realm_code) in active_by_user.items():
            existing = existing_by_user.get(user_id)
            if (
                existing is not None
                and (
                    str(existing['realm_code']) != realm_code
                    or (
                        existing['active_adventurer_id'] is not None
                        and int(existing['active_adventurer_id']) != adventurer_id
                    )
                )
            ):
                conflicts += 1
                continue
            if existing is None:
                connection.execute(text(
                    f'INSERT INTO {_TABLE} '
                    '(user_id, realm_code, assignment_reason, slot_state, '
                    'claim_token, claim_reservation_id, active_adventurer_id, '
                    'created_at, updated_at) '
                    'VALUES (:user_id, :realm_code, :reason, :state, NULL, NULL, '
                    ':adventurer_id, :created_at, :updated_at)'
                ), {
                    'user_id': user_id,
                    'realm_code': realm_code,
                    'reason': 'living_adventurer_backfill',
                    'state': 'active',
                    'adventurer_id': adventurer_id,
                    'created_at': migrated_at,
                    'updated_at': migrated_at,
                })
                inserted += 1
            else:
                if (
                    str(existing['realm_code']) == realm_code
                    and str(existing['slot_state']) == 'active'
                    and existing['claim_token'] is None
                    and existing['claim_reservation_id'] is None
                    and existing['active_adventurer_id'] is not None
                    and int(existing['active_adventurer_id']) == adventurer_id
                    and existing['reconcile_absent_since'] is None
                ):
                    continue
                connection.execute(text(
                    f'UPDATE {_TABLE} SET realm_code = :realm_code, '
                    'assignment_reason = :reason, slot_state = :state, '
                    'claim_token = NULL, claim_reservation_id = NULL, '
                    'active_adventurer_id = :adventurer_id, '
                    'reconcile_absent_since = NULL, updated_at = :updated_at '
                    'WHERE user_id = :user_id'
                ), {
                    'user_id': user_id,
                    'realm_code': realm_code,
                    'reason': 'living_adventurer_backfill',
                    'state': 'active',
                    'adventurer_id': adventurer_id,
                    'updated_at': migrated_at,
                })
                updated += 1
        if conflicts:
            raise RuntimeError(
                'S1 active-slot migration found a cross-realm living-role conflict'
            )

        active_user_ids = set(active_by_user)
        legacy_region_2_ids = [
            int(row['user_id'])
            for row in existing_rows
            if int(row['user_id']) not in active_user_ids
            and str(row['realm_code']) == 'region_2'
            and str(row['slot_state'] or 'active') == 'active'
            and row['active_adventurer_id'] is None
        ]
        preserved_region_2 = 0
        if legacy_region_2_ids:
            bind_names = ', '.join(
                f':legacy_region_2_{index}'
                for index in range(len(legacy_region_2_ids))
            )
            params = {
                f'legacy_region_2_{index}': user_id
                for index, user_id in enumerate(legacy_region_2_ids)
            }
            result = connection.execute(text(
                f'UPDATE {_TABLE} SET slot_state = :pending, '
                "assignment_reason = 'legacy_region_2_reconcile_required', "
                'claim_token = NULL, claim_reservation_id = NULL, '
                'reconcile_absent_since = NULL, '
                'updated_at = COALESCE(updated_at, created_at, :migrated_at) '
                f'WHERE user_id IN ({bind_names})'
            ), {
                **params,
                'pending': 'pending',
                'migrated_at': migrated_at,
            })
            preserved_region_2 = max(0, int(result.rowcount or 0))

        stale_legacy_ids = [
            int(row['user_id'])
            for row in existing_rows
            if int(row['user_id']) not in active_user_ids
            and str(row['realm_code']) == 'region_1'
            and str(row['slot_state'] or 'active') == 'active'
            and row['active_adventurer_id'] is None
        ]
        removed = 0
        if stale_legacy_ids:
            bind_names = ', '.join(
                f':stale_{index}' for index in range(len(stale_legacy_ids))
            )
            params = {
                f'stale_{index}': user_id
                for index, user_id in enumerate(stale_legacy_ids)
            }
            result = connection.execute(text(
                f'DELETE FROM {_TABLE} WHERE user_id IN ({bind_names})'
            ), params)
            removed = max(0, int(result.rowcount or 0))

    return {
        # Compatibility keys retained for startup logging and older operators.
        'history_users': len(active_by_user),
        'inserted': inserted,
        'conflicting_history': 0,
        'membership_history_conflicts': conflicts,
        'updated': updated,
        'removed_legacy_reservation_rows': removed,
        'preserved_legacy_region_2_rows': preserved_region_2,
    }


if __name__ == '__main__':
    migrate()
