"""Player projection of settled appearances while waiting for a draw."""

from collections import defaultdict

from db_repository.s1_reservation_history_repository import load_reservation_appearances
from plugins.adventurer_s1.services.s1_realm_service import S1RealmService
from utils.pagination import build_paginated_response
from utils.time_utils import to_utc_iso


def get_reservation_appearance_history(user_id, *, page, per_page):
    rows, records, total, page = load_reservation_appearances(
        user_id, S1RealmService.current_runtime_realm(), page=page, per_page=per_page,
    )
    excerpts = defaultdict(list)
    for record in records:
        key = (record.actor_id, record.incarnation, record.expedition_log_id)
        if record.excerpt not in excerpts[key]:
            excerpts[key].append(record.excerpt)
    items = [{
        'incarnation': row.incarnation,
        'appeared_at': to_utc_iso(row.created_at),
        'location': row.temporary_location_name or row.hidden_dungeon or row.target_dungeon,
        'party_name': row.party_name,
        'excerpts': excerpts[(row.actor_id, row.incarnation, row.expedition_log_id)],
    } for row in rows]
    return build_paginated_response(items, total, page, per_page)
