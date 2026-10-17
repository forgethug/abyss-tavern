"""Persist the existing hot/cold identity projection instead of reparsing prose.

PostgreSQL owns invalidation so ORM, bulk, Core and administrator writes have
the same behavior. Installation never backfills the table at application start.
Operators may call the appropriate backfill helper with its returned cursor; each page
commits independently and changes only derived metadata, not log timestamps.
"""

import json

from sqlalchemy import inspect, text

from extensions import db


TABLE = 'adventurer_s1_expedition_logs'
COLUMN = 'player_name_history'
TRIGGER = 'trg_advs1_player_name_history_v1'

# Keep the member map as JSON: converting it to JSONB changes key order and
# therefore alias allocation. These are exactly the existing hot-read fields,
# including both primary and legacy audit names when a row contains both.
PROJECTION_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION advs1_player_name_history_v1(payload json)
RETURNS json LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $projection$
    SELECT json_build_object(
        'member_names', fields._member_id_to_name,
        'names', jsonb_path_query_array(fields.audit_member_snapshot, '$[*].adventurer_name'),
        'legacy_names', jsonb_path_query_array(fields.audit_member_snapshot, '$[*].name'),
        'guest_names', jsonb_path_query_array(fields.waiting_player_npc_injection, '$.candidates[*].display_name'),
        'mirror_name', fields.cross_realm_mirror_injection -> 'candidate' -> 'display_name'
    )
    FROM json_to_record(CASE WHEN json_typeof(payload) = 'object' THEN payload END)
    AS fields(_member_id_to_name json, audit_member_snapshot jsonb,
              waiting_player_npc_injection jsonb, cross_realm_mirror_injection json)
$projection$
"""

TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION advs1_set_player_name_history_v2()
RETURNS trigger LANGUAGE plpgsql AS $trigger$
BEGIN
    IF NEW.cold_compressed_at IS NULL THEN
        NEW.player_name_history := advs1_player_name_history_v1(NEW.full_log);
    ELSIF json_typeof(NEW.full_log -> '__adventurer_s1_zlib_v1__') = 'string'
          AND json_typeof(NEW.full_log -> '_player_name_history_v1') = 'object' THEN
        NEW.player_name_history := NEW.full_log -> '_player_name_history_v1';
    ELSE
        -- Legacy cold envelopes retain their original decoder and name rules.
        NEW.player_name_history := NULL;
    END IF;
    RETURN NEW;
END;
$trigger$
"""

CREATE_TRIGGER_SQL = f"""
CREATE TRIGGER {TRIGGER}
BEFORE INSERT OR UPDATE OF full_log, cold_compressed_at ON {TABLE}
FOR EACH ROW EXECUTE FUNCTION advs1_set_player_name_history_v2()
"""


def _trigger_ready(connection):
    row = connection.execute(text("""
        SELECT t.tgenabled, t.tgtype, p.proname,
               ARRAY(SELECT a.attname FROM unnest(t.tgattr::smallint[]) AS n
                     JOIN pg_attribute a ON a.attrelid=t.tgrelid AND a.attnum=n
                     ORDER BY a.attname) AS columns
        FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
        WHERE t.tgrelid=to_regclass(:table) AND t.tgname=:trigger
    """), {'table': TABLE, 'trigger': TRIGGER}).mappings().first()
    if row is None:
        return False
    if not (row['tgenabled'] == 'O' and row['tgtype'] == 23
            and row['proname'] in ('advs1_set_player_name_history_v1', 'advs1_set_player_name_history_v2')
            and list(row['columns']) == ['cold_compressed_at', 'full_log']):
        raise RuntimeError(f'{TRIGGER}: unexpected trigger definition; inspect before recovery')
    return row['proname'] == 'advs1_set_player_name_history_v2'


def migrate(*, engine=None):
    """Idempotent nullable-column/trigger installation, with bounded DDL locks."""
    bind = engine or db.engine
    with bind.begin() as connection:
        if not inspect(connection).has_table(TABLE):
            return
        if bind.dialect.name == 'postgresql':
            connection.execute(text("SET LOCAL lock_timeout='3s'"))
            connection.execute(text("SET LOCAL statement_timeout='10s'"))
            # Several workers can initialize together; serialize only this DDL.
            connection.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                               {'key': TRIGGER})
        columns = {column['name'] for column in inspect(connection).get_columns(TABLE)}
        if COLUMN not in columns:
            connection.execute(text(f'ALTER TABLE {TABLE} ADD COLUMN {COLUMN} JSON'))
        if bind.dialect.name == 'postgresql' and not _trigger_ready(connection):
            connection.execute(text(PROJECTION_FUNCTION_SQL))
            connection.execute(text(TRIGGER_FUNCTION_SQL))
            connection.execute(text(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {TABLE}'))
            connection.execute(text(CREATE_TRIGGER_SQL))
            _trigger_ready(connection)


def backfill_page(*, after_id=0, through_id, page_size, engine=None):
    """Fill one bounded page from the locked current row, safe against rewrites.

    Choose a fixed through_id before starting a run. A lock/statement timeout
    rolls back the entire page: retry the same cursor, never skip those records.
    Already projected and cold records are excluded before the page limit.
    """
    bind = engine or db.engine
    if bind.dialect.name != 'postgresql':
        raise RuntimeError('The persisted identity projection requires PostgreSQL')
    if page_size < 1 or after_id < 0 or through_id < 0:
        raise ValueError('Expected a positive page_size and nonnegative cursors')
    with bind.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='1s'"))
        connection.execute(text("SET LOCAL statement_timeout='8s'"))
        # Lock only the candidate rows in ID order before computing from them.
        # Concurrent full_log writes either precede this snapshot or follow its
        # commit and refresh the projection through the trigger.
        rows = connection.execute(text(f"""
            WITH page AS MATERIALIZED (
                SELECT id FROM {TABLE}
                WHERE id > :after_id AND id <= :through_id
                  AND cold_compressed_at IS NULL AND {COLUMN} IS NULL
                ORDER BY id LIMIT :page_size
            ), locked AS MATERIALIZED (
                SELECT log.id, log.full_log
                FROM {TABLE} log JOIN page USING (id)
                WHERE log.cold_compressed_at IS NULL AND log.{COLUMN} IS NULL
                ORDER BY log.id FOR UPDATE OF log
            ), updated AS (
                UPDATE {TABLE} log
                SET {COLUMN}=advs1_player_name_history_v1(locked.full_log)
                FROM locked WHERE log.id=locked.id RETURNING log.id
            )
            SELECT (SELECT max(id) FROM page) AS next_id,
                   (SELECT count(*) FROM page) AS scanned,
                   (SELECT count(*) FROM updated) AS updated
        """), {'after_id': after_id, 'through_id': through_id,
               'page_size': page_size}).mappings().one()
    return dict(rows)


def backfill_cold_page(*, after_id=0, through_id, page_size, engine=None):
    """Fill a bounded cold page; decode with no connection, then compare/write.

    Only derived metadata changes. Concurrent edits are reported as conflicts,
    never overwritten. A timeout aborts the page; retry the same cursor. After a
    pass with conflicts, start another pass from zero to revisit missing rows.
    This explicit maintenance entry point is never run by startup or GET.
    """
    from plugins.adventurer_s1.cold_storage import ColdCompressedJSON, cold_player_name_history

    bind = engine or db.engine
    if bind.dialect.name != 'postgresql':
        raise RuntimeError('The persisted identity projection requires PostgreSQL')
    if page_size < 1 or after_id < 0 or through_id < 0:
        raise ValueError('Expected a positive page_size and nonnegative cursors')
    with bind.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout='8s'"))
        rows = connection.execute(text(f"""
            SELECT id, full_log, md5(full_log::text) AS version_hash
            FROM {TABLE}
            WHERE id > :after_id AND id <= :through_id
              AND cold_compressed_at IS NOT NULL AND {COLUMN} IS NULL
            ORDER BY id LIMIT :page_size
        """), {'after_id': after_id, 'through_id': through_id,
               'page_size': page_size}).mappings().all()
    if not rows:
        return {'next_id': None, 'scanned': 0, 'updated': 0, 'conflicted': 0}

    decoder = ColdCompressedJSON()
    changes = [
        {'id': row['id'], 'version_hash': row['version_hash'],
         'projection': cold_player_name_history(decoder.process_result_value(row['full_log'], None))}
        for row in rows
    ]
    with bind.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='1s'"))
        connection.execute(text("SET LOCAL statement_timeout='8s'"))
        # Lock in ID order, then recheck the *current* row after any concurrent
        # writer. Keep projection as JSON to preserve member-map insertion order.
        updated = connection.execute(text(f"""
            WITH changes AS MATERIALIZED (
                SELECT * FROM json_to_recordset(CAST(:changes AS json))
                AS r(id integer, version_hash text, projection json)
            ), locked AS MATERIALIZED (
                SELECT log.id, changes.projection FROM {TABLE} log
                JOIN changes ON changes.id=log.id
                WHERE log.cold_compressed_at IS NOT NULL AND log.{COLUMN} IS NULL
                  AND md5(log.full_log::text)=changes.version_hash
                ORDER BY log.id FOR UPDATE OF log
            ), updated AS (
                UPDATE {TABLE} log SET {COLUMN}=locked.projection
                FROM locked WHERE log.id=locked.id RETURNING log.id
            )
            SELECT count(*) FROM updated
        """), {'changes': json.dumps(changes, ensure_ascii=False)}).scalar_one()
    return {'next_id': rows[-1]['id'], 'scanned': len(rows), 'updated': updated,
            'conflicted': len(rows) - updated}
