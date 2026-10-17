"""Short database operations used while an S1 expedition is being generated."""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from dataclasses import dataclass
import json

from sqlalchemy import JSON, case, cast, column, func, literal, or_, select, true, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB

from db_repository import db_manager
from db_repository.exceptions import DatabaseError
from extensions import db
from plugins.adventurer_s1.cold_storage import ColdCompressedJSON, _JSON_MARKER
from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog as Log


@dataclass(frozen=True)
class LockedGenerationCheckpoint:
    id: int
    party_id: int | None
    status: str
    generation_owner_boot_id: str | None
    full_log: dict
    replace_full_log: bool


def _generation_checkpoint_statement(log_id: int, checkpoint_key: str, dialect: str):
    """Lock the authoritative row but transfer only checkpoint data for hot logs."""
    payload = type_coerce(Log.full_log, JSON)
    if dialect == 'postgresql':
        is_object = func.json_typeof(payload) == 'object'
        is_cold = payload[_JSON_MARKER].is_not(None)
    else:
        is_object = func.json_type(payload) == 'object'
        is_cold = func.json_type(payload, f'$."{_JSON_MARKER}"').is_not(None)
    needs_full_log = or_(~is_object, is_cold)
    return select(
        Log.id, Log.party_id, Log.status, Log.generation_owner_boot_id,
        payload[checkpoint_key].label('checkpoint'),
        needs_full_log.label('needs_full_log'),
        case((needs_full_log, payload), else_=None).label('legacy_full_log'),
    ).where(Log.id == log_id).with_for_update(of=Log)


def load_generation_checkpoint_for_update(session, log_id: int, checkpoint_key: str):
    """Use the caller's transaction/lock; do not open another Session or commit."""
    row = session.execute(_generation_checkpoint_statement(
        log_id, checkpoint_key, session.get_bind().dialect.name,
    )).first()
    if row is None:
        return None
    if row.needs_full_log:
        # Rare historical/cold rows retain the old decoder and error behavior.
        # Never merge new live keys into a compressed envelope.
        payload = ColdCompressedJSON().process_result_value(row.legacy_full_log, None)
        full_log = dict(payload or {})
    else:
        full_log = {checkpoint_key: row.checkpoint}
    return LockedGenerationCheckpoint(
        id=row.id, party_id=row.party_id, status=row.status,
        generation_owner_boot_id=row.generation_owner_boot_id,
        full_log=full_log, replace_full_log=bool(row.needs_full_log),
    )


def _generation_checkpoint_update_statement(
    state: LockedGenerationCheckpoint, changes: dict, *, phase: str,
    owner: str, phase_at: datetime, dialect: str,
):
    payload = type_coerce(Log.full_log, JSON)
    if state.replace_full_log:
        new_payload = changes
    elif dialect == 'postgresql':
        # Merge top-level fields only: a checkpoint replaces its prior version,
        # while unrelated log fields written before our row lock remain intact.
        new_payload = cast(
            cast(payload, JSONB).op('||')(cast(literal(changes, type_=JSON), JSONB)), JSON,
        )
    else:
        args = [payload]
        for key, value in changes.items():
            args.extend((f'$.{json.dumps(key)}', func.json(literal(value, type_=JSON))))
        # json_patch recursively merges and removes JSON nulls; json_set instead
        # replaces each whole field, matching PostgreSQL's top-level merge.
        new_payload = func.json_set(*args)
    return update(Log).where(
        Log.id == state.id, Log.party_id == state.party_id,
        Log.status == state.status,
        Log.generation_owner_boot_id == state.generation_owner_boot_id,
    ).values(
        full_log=new_payload, generation_phase=phase,
        generation_phase_at=phase_at, generation_owner_boot_id=owner,
    ).execution_options(synchronize_session=False)


def write_generation_checkpoint(session, state, changes, *, phase, owner, phase_at) -> bool:
    """Write under the same row lock; transaction owner commits checkpoint+phase."""
    return session.execute(_generation_checkpoint_update_statement(
        state, changes, phase=phase, owner=owner, phase_at=phase_at,
        dialect=session.get_bind().dialect.name,
    )).rowcount == 1


def touch_generation_lease(log_id: int, claim_token: str) -> bool:
    """Atomically renew one owned claim without waiting for a separate COMMIT.

    This standalone heartbeat has no companion writes. AUTOCOMMIT lets PostgreSQL
    finish the UPDATE and release its row lock even if the worker is delayed
    before it can consume the result. Callers must not use it to commit business
    state or renew a claim inside a larger transaction.
    """
    if not claim_token:
        return False
    statement = update(Log).where(
        Log.id == log_id,
        Log.status == 'generating',
        Log.generation_owner_boot_id == claim_token,
    ).values(generation_phase_at=_portable_utc_now())
    try:
        with db.engine.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            return connection.execute(statement).rowcount == 1
    except Exception as exc:
        # Keep the existing error category and DBAPI cause for the generation
        # guard and heartbeat logger; a disconnect is not a lost-claim result.
        raise DatabaseError(f'Transaction failed: {exc}', original_error=exc) from exc


def _waiting_npc_snapshot_statement(log_id: int, dialect: str):
    """Select the frozen field, or its cold envelope, without the rest of the log."""
    payload = type_coerce(Log.full_log, JSON)
    if dialect == 'postgresql':
        object_payload = case(
            (func.json_typeof(payload) == 'object', payload), else_=None,
        )
        fields = func.json_to_record(object_payload).table_valued(
            column('waiting_player_npc_injection', JSON),
            column(_JSON_MARKER, JSON),
            column('sha256', JSON),
        ).render_derived(with_types=True).lateral('waiting_npc_snapshot')
        marker = fields.c[_JSON_MARKER]
        return select(
            fields.c.waiting_player_npc_injection, marker, fields.c.sha256,
            # json_to_record maps JSON null to SQL NULL. Check key presence on
            # the raw JSON so malformed cold envelopes still fail decoding.
            payload[_JSON_MARKER].is_not(None),
        ).select_from(Log).outerjoin(fields, true()).where(Log.id == log_id)
    return select(
        payload['waiting_player_npc_injection'], payload[_JSON_MARKER],
        payload['sha256'],
        func.json_type(payload, f'$."{_JSON_MARKER}"').is_not(None),
    ).where(Log.id == log_id)


def load_frozen_waiting_npc_injection(log_id: int) -> dict | None:
    """Release the read connection before decoding any archived log payload."""
    with db_manager.readonly_snapshot() as connection:
        row = connection.execute(
            _waiting_npc_snapshot_statement(log_id, connection.dialect.name),
        ).first()
    if row is None:
        return None
    snapshot, marker, checksum, is_cold = row
    if is_cold:
        # Detect the actual envelope, including legacy rows without a cold date.
        # Corrupt envelopes still raise; never substitute a live-looking field.
        payload = ColdCompressedJSON().process_result_value(
            {_JSON_MARKER: marker, 'sha256': checksum}, None,
        )
        snapshot = (
            payload.get('waiting_player_npc_injection')
            if isinstance(payload, dict) else None
        )
    return dict(snapshot) if isinstance(snapshot, dict) else None
