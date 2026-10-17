"""Repository helpers for the S1 Star Lantern Ceremony Hall."""

from sqlalchemy import and_, case, func

from extensions import db
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1CeremonyAttendance,
    AdventurerS1CeremonyProfile,
    AdventurerS1CeremonyVisit,
    AdventurerS1ItemLog,
    AdventurerS1Party,
    AdventurerS1PartyMember,
)


ACTIVE_PARTY_STATUSES = ('forming', 'exploring')


def get_player_state(
    user_id: int,
    *,
    ceremony_type: str,
    occurrence_year: int,
):
    """Load the account, birthday profile, and this year's receipt in one query."""
    return (
        db.session.query(
            User.username,
            User.nickname,
            AdventurerS1CeremonyProfile,
            AdventurerS1CeremonyAttendance,
        )
        .outerjoin(
            AdventurerS1CeremonyProfile,
            AdventurerS1CeremonyProfile.user_id == User.id,
        )
        .outerjoin(
            AdventurerS1CeremonyAttendance,
            and_(
                AdventurerS1CeremonyAttendance.user_id == User.id,
                AdventurerS1CeremonyAttendance.ceremony_type == ceremony_type,
                AdventurerS1CeremonyAttendance.occurrence_year == occurrence_year,
            ),
        )
        .filter(User.id == int(user_id))
        .first()
    )


def lock_user(user_id: int):
    """Use the stable account row as the per-player ceremony write lock."""
    return (
        db.session.query(User.id, User.username, User.nickname)
        .filter(User.id == int(user_id))
        .with_for_update()
        .first()
    )


def get_profile(user_id: int) -> AdventurerS1CeremonyProfile | None:
    return (
        db.session.query(AdventurerS1CeremonyProfile)
        .filter(AdventurerS1CeremonyProfile.user_id == int(user_id))
        .first()
    )


def save_birthday_profile(
    profile: AdventurerS1CeremonyProfile | None,
    user_id: int,
    *,
    birthday_month: int,
    birthday_day: int,
) -> AdventurerS1CeremonyProfile:
    if profile is None:
        profile = AdventurerS1CeremonyProfile(
            user_id=int(user_id),
            birthday_month=int(birthday_month),
            birthday_day=int(birthday_day),
        )
        db.session.add(profile)
    else:
        profile.birthday_month = int(birthday_month)
        profile.birthday_day = int(birthday_day)
    db.session.flush()
    return profile


def get_attendance(
    user_id: int,
    *,
    ceremony_type: str,
    occurrence_year: int,
    for_update: bool = False,
) -> AdventurerS1CeremonyAttendance | None:
    query = (
        db.session.query(AdventurerS1CeremonyAttendance)
        .filter(
            AdventurerS1CeremonyAttendance.user_id == int(user_id),
            AdventurerS1CeremonyAttendance.ceremony_type == ceremony_type,
            AdventurerS1CeremonyAttendance.occurrence_year == int(occurrence_year),
        )
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def _get_owned_adventurer_identity(user_id: int):
    return (
        db.session.query(AdventurerS1.id, AdventurerS1.name)
        .filter(
            AdventurerS1.owner_id == int(user_id),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )
        .order_by(AdventurerS1.id.desc())
        .first()
    )


def get_owned_adventurer_name(user_id: int) -> str | None:
    """Return the player's current in-world identity for ceremony projections."""
    owned_adventurer = _get_owned_adventurer_identity(user_id)
    if owned_adventurer is None:
        return None
    return str(owned_adventurer.name or '').strip()[:100] or None


def lock_owned_adventurer(user_id: int) -> AdventurerS1 | None:
    """Lock the player's current active adventurer for a caller-owned grant transaction."""
    return (
        db.session.query(AdventurerS1)
        .filter(
            AdventurerS1.owner_id == int(user_id),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )
        .order_by(AdventurerS1.id.desc())
        .with_for_update()
        .first()
    )


def get_ceremony_gift_log(
    attendance_id: int,
    *,
    item_name: str,
    source_type: str,
    related_type: str,
) -> AdventurerS1ItemLog | None:
    """Return the durable item-grant receipt for one ceremony attendance."""
    return (
        db.session.query(AdventurerS1ItemLog)
        .filter(
            AdventurerS1ItemLog.item_name == item_name,
            AdventurerS1ItemLog.source_type == source_type,
            AdventurerS1ItemLog.related_id == str(int(attendance_id)),
            AdventurerS1ItemLog.related_type == related_type,
            AdventurerS1ItemLog.change_type.in_(('gain', 'overflow')),
        )
        .order_by(AdventurerS1ItemLog.id.asc())
        .first()
    )


def mark_attendance_gift_delivered(
    attendance: AdventurerS1CeremonyAttendance,
    *,
    adventurer_id: int,
    destination: str,
    claimed_at,
) -> AdventurerS1CeremonyAttendance:
    """Persist the annual grant receipt on the ceremony record itself."""
    if destination not in {'equipment', 'warehouse'}:
        raise ValueError('Unsupported ceremony gift destination')
    attendance.gift_claimed_at = claimed_at
    attendance.gift_destination = destination
    attendance.gift_adventurer_id = int(adventurer_id)
    db.session.flush()
    return attendance


def get_banquet_companion_context(user_id: int) -> dict:
    """Load the owned adventurer and current party roster with bounded scalar queries."""
    owned_adventurer = _get_owned_adventurer_identity(user_id)
    if owned_adventurer is None:
        return {
            'owned_adventurer_id': None,
            'party_name': None,
            'members': [],
        }

    party = (
        db.session.query(
            AdventurerS1Party.id,
            AdventurerS1Party.name,
            AdventurerS1Party.target_size,
        )
        .join(
            AdventurerS1PartyMember,
            AdventurerS1PartyMember.party_id == AdventurerS1Party.id,
        )
        .filter(
            AdventurerS1PartyMember.adventurer_id == owned_adventurer.id,
            AdventurerS1Party.status.in_(ACTIVE_PARTY_STATUSES),
        )
        .order_by(
            case(
                (AdventurerS1Party.status == 'exploring', 0),
                else_=1,
            ),
            AdventurerS1Party.id.desc(),
        )
        .first()
    )

    if party is None:
        members = [owned_adventurer]
        party_name = None
    else:
        guest_limit = max(1, int(party.target_size or 1))
        members = (
            db.session.query(AdventurerS1.id, AdventurerS1.name)
            .join(
                AdventurerS1PartyMember,
                AdventurerS1PartyMember.adventurer_id == AdventurerS1.id,
            )
            .filter(
                AdventurerS1PartyMember.party_id == party.id,
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(
                AdventurerS1PartyMember.created_at.asc(),
                AdventurerS1PartyMember.id.asc(),
            )
            .limit(guest_limit)
            .all()
        )
        party_name = str(party.name or '').strip()[:100] or None

    member_rows = [
        {
            'adventurer_id': int(member.id),
            'name': str(member.name or '').strip()[:100],
        }
        for member in members
        if str(member.name or '').strip()
    ]
    owned_id = int(owned_adventurer.id)
    if not any(member['adventurer_id'] == owned_id for member in member_rows):
        member_rows.insert(0, {
            'adventurer_id': owned_id,
            'name': str(owned_adventurer.name or '').strip()[:100],
        })

    return {
        'owned_adventurer_id': owned_id,
        'party_name': party_name,
        'members': member_rows,
    }


def create_attendance(
    *,
    user_id: int,
    ceremony_type: str,
    occurrence_year: int,
    birthday_month: int,
    birthday_day: int,
    wish_key: str,
    celebrant_name: str,
    host_snapshot: dict,
    guest_snapshot: list[dict],
    feast_snapshot: dict | list[dict],
    tavern_blessing: str,
    visit_token: str,
    visitors_open: bool,
    visitors_opened_at,
    attended_at,
) -> AdventurerS1CeremonyAttendance:
    attendance = AdventurerS1CeremonyAttendance(
        user_id=int(user_id),
        ceremony_type=ceremony_type,
        occurrence_year=int(occurrence_year),
        birthday_month=int(birthday_month),
        birthday_day=int(birthday_day),
        wish_key=wish_key,
        celebrant_name=celebrant_name,
        host_snapshot=host_snapshot,
        guest_snapshot=guest_snapshot,
        feast_snapshot=feast_snapshot,
        tavern_blessing=tavern_blessing,
        visit_token=visit_token,
        visitors_open=bool(visitors_open),
        visitors_opened_at=visitors_opened_at,
        attended_at=attended_at,
    )
    db.session.add(attendance)
    db.session.flush()
    return attendance


def get_attendance_by_visit_token(
    visit_token: str,
    *,
    for_update: bool = False,
) -> AdventurerS1CeremonyAttendance | None:
    query = (
        db.session.query(AdventurerS1CeremonyAttendance)
        .filter(AdventurerS1CeremonyAttendance.visit_token == visit_token)
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def set_attendance_visitors_open(
    attendance: AdventurerS1CeremonyAttendance,
    *,
    visitors_open: bool,
    visit_token: str,
    opened_at,
) -> AdventurerS1CeremonyAttendance:
    attendance.visit_token = visit_token
    attendance.visitors_open = bool(visitors_open)
    attendance.visitors_opened_at = opened_at if visitors_open else None
    db.session.flush()
    return attendance


def count_visits(attendance_id: int) -> int:
    value = (
        db.session.query(func.count(AdventurerS1CeremonyVisit.id))
        .filter(AdventurerS1CeremonyVisit.attendance_id == int(attendance_id))
        .scalar()
    )
    return int(value or 0)


def list_open_birthday_tables(
    *,
    ceremony_type: str,
    day_start_utc,
    day_end_utc,
    page: int,
    per_page: int,
):
    filters = (
        AdventurerS1CeremonyAttendance.ceremony_type == ceremony_type,
        AdventurerS1CeremonyAttendance.visitors_open.is_(True),
        AdventurerS1CeremonyAttendance.visit_token.isnot(None),
        AdventurerS1CeremonyAttendance.attended_at >= day_start_utc,
        AdventurerS1CeremonyAttendance.attended_at < day_end_utc,
    )
    total = int(
        db.session.query(func.count(AdventurerS1CeremonyAttendance.id))
        .filter(*filters)
        .scalar()
        or 0
    )
    visit_counts = (
        db.session.query(
            AdventurerS1CeremonyVisit.attendance_id.label('attendance_id'),
            func.count(AdventurerS1CeremonyVisit.id).label('visit_count'),
        )
        .group_by(AdventurerS1CeremonyVisit.attendance_id)
        .subquery()
    )
    rows = (
        db.session.query(
            AdventurerS1CeremonyAttendance,
            func.coalesce(visit_counts.c.visit_count, 0),
        )
        .outerjoin(
            visit_counts,
            visit_counts.c.attendance_id == AdventurerS1CeremonyAttendance.id,
        )
        .filter(*filters)
        .order_by(
            AdventurerS1CeremonyAttendance.visitors_opened_at.asc(),
            AdventurerS1CeremonyAttendance.id.asc(),
        )
        .offset((int(page) - 1) * int(per_page))
        .limit(int(per_page))
        .all()
    )
    return rows, total


def list_visits(
    attendance_id: int,
    *,
    page: int,
    per_page: int,
):
    query = (
        db.session.query(AdventurerS1CeremonyVisit)
        .filter(AdventurerS1CeremonyVisit.attendance_id == int(attendance_id))
    )
    total = int(query.count())
    rows = (
        query
        .order_by(
            AdventurerS1CeremonyVisit.created_at.desc(),
            AdventurerS1CeremonyVisit.id.desc(),
        )
        .offset((int(page) - 1) * int(per_page))
        .limit(int(per_page))
        .all()
    )
    return rows, total


def get_latest_visit_for_user(
    attendance_id: int,
    visitor_user_id: int,
) -> AdventurerS1CeremonyVisit | None:
    return (
        db.session.query(AdventurerS1CeremonyVisit)
        .filter(
            AdventurerS1CeremonyVisit.attendance_id == int(attendance_id),
            AdventurerS1CeremonyVisit.visitor_user_id == int(visitor_user_id),
        )
        .order_by(
            AdventurerS1CeremonyVisit.created_at.desc(),
            AdventurerS1CeremonyVisit.id.desc(),
        )
        .first()
    )


def get_visit_by_request_token(
    attendance_id: int,
    visitor_user_id: int,
    request_token: str,
) -> AdventurerS1CeremonyVisit | None:
    return (
        db.session.query(AdventurerS1CeremonyVisit)
        .filter(
            AdventurerS1CeremonyVisit.attendance_id == int(attendance_id),
            AdventurerS1CeremonyVisit.visitor_user_id == int(visitor_user_id),
            AdventurerS1CeremonyVisit.request_token == request_token,
        )
        .first()
    )


def count_visits_for_user(attendance_id: int, visitor_user_id: int) -> int:
    value = (
        db.session.query(func.count(AdventurerS1CeremonyVisit.id))
        .filter(
            AdventurerS1CeremonyVisit.attendance_id == int(attendance_id),
            AdventurerS1CeremonyVisit.visitor_user_id == int(visitor_user_id),
        )
        .scalar()
    )
    return int(value or 0)


def create_visit(
    *,
    attendance_id: int,
    visitor_user_id: int,
    visitor_name: str,
    action_key: str,
    message: str | None,
    request_token: str,
    created_at,
) -> AdventurerS1CeremonyVisit:
    visit = AdventurerS1CeremonyVisit(
        attendance_id=int(attendance_id),
        visitor_user_id=int(visitor_user_id),
        visitor_name=visitor_name,
        action_key=action_key,
        message=message,
        request_token=request_token,
        created_at=created_at,
    )
    db.session.add(visit)
    db.session.flush()
    return visit
