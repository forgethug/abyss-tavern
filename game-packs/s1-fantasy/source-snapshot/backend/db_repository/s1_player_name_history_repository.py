"""Frozen player identities from the histories actually feeding an expedition."""

from sqlalchemy import (
    JSON, and_, bindparam, case, cast, column, func, or_, select, true, tuple_, type_coerce, union,
)
from sqlalchemy.dialects.postgresql import JSONB

from extensions import db


def _name_array(payload, path: str, field: str, dialect: str):
    """Project names without transferring asset descriptions or NPC profiles."""
    if dialect == 'postgresql':
        # CASE with the persisted JSON projection needs an actual JSON SQL
        # type, not just SQLAlchemy result typing. Cast only the tiny array.
        return cast(func.jsonb_path_query_array(
            payload, f'{path}[*].{field}', type_=JSON,
        ), JSON)
    # SQLite is used by the focused repository fixtures.
    entries = func.json_each(payload, path).table_valued('value')
    return type_coerce(select(func.json_group_array(
        func.json_extract(entries.c.value, f'$.{field}'),
    )).select_from(entries).scalar_subquery(), JSON)


def _history_log_id_page(sources, expedition_log_id: int, page_size: int):
    """Bound each identity relation before merging; never page through log JSON."""
    pages = []
    for source in sources:
        log_id = source.selected_columns.log_id
        page = source.where(
            log_id > bindparam('after_log_id'), log_id < expedition_log_id,
        ).distinct().order_by(log_id).limit(page_size).subquery()
        pages.append(select(page.c.log_id))
    # A source's first N distinct IDs contain every ID it can contribute to the
    # union's first N. Deduplicate both within sources and across sources so an
    # overlapping member/location/NPC history cannot starve later pages.
    related = union(*pages).subquery()
    return select(related.c.log_id).order_by(related.c.log_id).limit(page_size)


def _history_name_page(Log, log_ids, dialect: str):
    """Project identities for a bounded ID list or scalar ID-page SELECT."""
    # Change the SQLAlchemy result type, not the SQL value (SQLite CAST AS JSON
    # would turn objects into numbers). Cold decoding belongs outside the read.
    payload = type_coerce(Log.full_log, JSON)
    projection = Log.player_name_history
    cold_payload = case(
        (projection.is_not(None), None),
        (Log.cold_compressed_at.is_not(None), payload),
        else_=None,
    )

    def projected(key, fallback):
        return case((projection.is_not(None), projection[key]), else_=fallback)

    if dialect == 'postgresql':
        # CASE must guard json_typeof too: even inspecting the type detoasts and
        # parses the huge document. AND does not guarantee evaluation order.
        hot_payload = case(
            (Log.cold_compressed_at.is_not(None), None),
            (projection.is_not(None), None),
            (func.json_typeof(payload) == 'object', payload),
            else_=None,
        )
        # A single lateral record scan extracts the needed branches. Converting
        # full_log to JSONB per array parses all prose/checkpoints repeatedly;
        # materializing that entire document instead can spill it to temp disk.
        fields = func.json_to_record(hot_payload).table_valued(
            # Keep JSON key order: it determines frozen-name alias allocation.
            column('_member_id_to_name', JSON),
            column('audit_member_snapshot', JSONB),
            column('waiting_player_npc_injection', JSONB),
            column('cross_realm_mirror_injection', JSON),
        ).render_derived(with_types=True).lateral('name_history_fields')

        return select(
            Log.id, projected('member_names', fields.c._member_id_to_name),
            projected('names', _name_array(
                fields.c.audit_member_snapshot, '$', 'adventurer_name', dialect,
            )),
            projected('legacy_names', _name_array(
                fields.c.audit_member_snapshot, '$', 'name', dialect,
            )),
            projected('guest_names', _name_array(
                fields.c.waiting_player_npc_injection, '$.candidates', 'display_name', dialect,
            )),
            projected('mirror_name', fields.c.cross_realm_mirror_injection['candidate']['display_name']),
            cold_payload,
        ).select_from(Log).outerjoin(fields, true()).where(Log.id.in_(log_ids)).order_by(Log.id)
    return select(
        Log.id, projected('member_names', payload['_member_id_to_name']),
        projected('names', _name_array(payload, '$.audit_member_snapshot', 'adventurer_name', dialect)),
        projected('legacy_names', _name_array(payload, '$.audit_member_snapshot', 'name', dialect)),
        projected('guest_names', _name_array(payload, '$.waiting_player_npc_injection.candidates', 'display_name', dialect)),
        projected('mirror_name', payload['cross_realm_mirror_injection']['candidate']['display_name']),
        cold_payload,
    ).where(Log.id.in_(log_ids)).order_by(Log.id)


def iter_player_name_history(
    *, expedition_log_id: int, member_ids: list[int], location_revisions: dict[int, int],
    npc_lives: list[tuple[int, int]], source_log_ids: list[int], page_size: int,
):
    """Yield identity snapshots in keyset pages, releasing the connection per page.

    Projected logs return only identity fields. Legacy cold logs must be decoded, but
    only for related source logs, and outside the read transaction. Never consult
    current player names: a rename must not change the meaning of old evidence.
    """
    from plugins.adventurer_s1.cold_storage import ColdCompressedJSON, cold_player_name_history
    from plugins.adventurer_s1.models import (
        AdventurerS1ExpeditionLog as Log,
        AdventurerS1ExpeditionLogMember as Member,
        AdventurerS1WaitingNpcRecord as Record,
        AdventurerS1WorldLocationStateHistory as LocationHistory,
    )

    sources = []
    if member_ids:
        sources.append(select(Member.expedition_log_id.label('log_id')).join(
            Log, Log.id == Member.expedition_log_id,
        ).where(
            Member.adventurer_id.in_(member_ids),
        ))
    if location_revisions:
        sources.append(select(LocationHistory.source_log_id.label('log_id')).join(
            Log, Log.id == LocationHistory.source_log_id,
        ).where(or_(
            *(and_(LocationHistory.location_state_id == location_id,
                   LocationHistory.revision <= revision)
              for location_id, revision in location_revisions.items()),
        )))
    if npc_lives:
        sources.append(select(Record.expedition_log_id.label('log_id')).join(
            Log, Log.id == Record.expedition_log_id,
        ).where(
            tuple_(Record.actor_id, Record.incarnation).in_(npc_lives),
        ))
    if source_log_ids:
        sources.append(select(Log.id.label('log_id')).where(Log.id.in_(source_log_ids)))
    if not sources:
        return

    # Filter missing source logs before each branch's LIMIT. Location/NPC source
    # IDs are not foreign keys; a page of removed logs must not end the scan.
    id_statement = _history_log_id_page(sources, expedition_log_id, page_size)
    statement = _history_name_page(Log, id_statement, db.engine.dialect.name)
    last_id = 0
    decoder = ColdCompressedJSON()
    while True:
        # The ID page and its identities share one statement snapshot. A
        # separate transaction/COMMIT adds no consistency and can remain
        # idle while a busy worker is delayed before consuming the result.
        with db.engine.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            rows = connection.execute(statement, {'after_log_id': last_id}).all()
        if not rows:
            return
        for log_id, member_names, names, legacy_names, guest_names, mirror_name, cold in rows:
            if cold is not None:
                identity = cold_player_name_history(decoder.process_result_value(cold, None))
                member_names = identity['member_names']
                names, legacy_names = identity['names'], identity['legacy_names']
                guest_names, mirror_name = identity['guest_names'], identity['mirror_name']
            yield {
                '_member_id_to_name': member_names,
                'member_alias_scope': [
                    {'name': name} for name in [
                        *(names or []), *(legacy_names or []), *(guest_names or []), mirror_name,
                    ]
                    if isinstance(name, str) and name
                ],
            }
        if len(rows) < page_size:
            return
        last_id = rows[-1][0]
