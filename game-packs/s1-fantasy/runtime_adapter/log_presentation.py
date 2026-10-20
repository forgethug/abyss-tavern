"""Add display names to admin log responses without changing stored evidence."""

from functools import wraps


def _member_id(value):
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value) if int(value) > 0 else None
    return None


def with_member_names(full_log):
    if not isinstance(full_log, dict):
        return full_log
    updates = full_log.get("member_updates")
    if not isinstance(updates, list):
        return full_log
    missing_ids = {
        member_id
        for update in updates
        if isinstance(update, dict) and not update.get("name")
        if (member_id := _member_id(update.get("id"))) is not None
    }
    if not missing_ids:
        return full_log

    from plugins.adventurer_s1.services.adventurer_s1_log_service import (
        _fetch_adventurer_names,
    )

    # This query uses the current runtime's database; never cache IDs across apps.
    names = _fetch_adventurer_names(sorted(missing_ids))
    return {
        **full_log,
        "member_updates": [
            {**update, "name": names[member_id]}
            if isinstance(update, dict)
            and not update.get("name")
            and (member_id := _member_id(update.get("id"))) in names
            and names[member_id]
            else update
            for update in updates
        ],
    }


def install(_app):
    from plugins.adventurer_s1.services.adventurer_s1_tavern_service import (
        AdventurerS1TavernService,
    )

    original = AdventurerS1TavernService.get_log_detail
    if getattr(original, "_portable_member_names", False):
        return

    @wraps(original)
    def get_log_detail(log_id, user_id=None, is_admin=False, reveal_full=False):
        data, error = original(
            log_id, user_id=user_id, is_admin=is_admin, reveal_full=reveal_full,
        )
        if error or not data or not is_admin:
            return data, error
        # Only enrich the already-authorized, already-filtered response.
        # Copy both containers so SQLAlchemy JSON state remains untouched.
        return {**data, "full_log": with_member_names(data.get("full_log"))}, error

    get_log_detail._portable_member_names = True
    AdventurerS1TavernService.get_log_detail = staticmethod(get_log_detail)
