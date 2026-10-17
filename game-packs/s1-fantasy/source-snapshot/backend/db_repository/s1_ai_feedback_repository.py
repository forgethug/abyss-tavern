"""Database access for S1 player AI feedback and admin aggregates."""
from runtime_core.time import utc_now as _portable_utc_now

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, func, or_, tuple_
from sqlalchemy.orm import joinedload

from extensions import db
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1AiFeedback,
    AdventurerS1BuildingUsageLog,
    AdventurerS1DataAppeal,
    AdventurerS1ExpeditionLog,
    AdventurerS1TokenUsage,
)


def get_owned_appeal(user_id: int, appeal_id: int) -> AdventurerS1DataAppeal | None:
    return db.session.query(AdventurerS1DataAppeal).filter(
        AdventurerS1DataAppeal.id == int(appeal_id),
        AdventurerS1DataAppeal.user_id == int(user_id),
    ).first()


def get_appeal_usage_map(specs: dict) -> dict[tuple[str, int], AdventurerS1TokenUsage]:
    """Select one successful receipt per visible appeal stage in one query."""
    if not specs:
        return {}
    usage = AdventurerS1TokenUsage
    predicates = []
    for (_, appeal_id), (call_types, before) in specs.items():
        conditions = [usage.appeal_id == appeal_id, usage.call_type.in_(call_types)]
        if before is not None:
            conditions.append(usage.created_at < before)
        predicates.append(and_(*conditions))
    latest = db.session.query(func.max(usage.id)).filter(
        usage.is_success.is_(True), or_(*predicates),
    ).group_by(usage.appeal_id, usage.call_type)
    rows = db.session.query(usage).filter(usage.id.in_(latest)).all()
    result = {}
    for key, (call_types, before) in specs.items():
        candidates = [row for row in rows if row.appeal_id == key[1]
                      and row.call_type in call_types
                      and (before is None or row.created_at < before)]
        if candidates:
            result[key] = max(candidates, key=lambda row: row.id)
    return result


def lock_feedback_author(user_id: int) -> bool:
    """Serialize upserts for one player across application workers."""
    row = (
        db.session.query(User.id)
        .filter(User.id == int(user_id))
        .with_for_update()
        .first()
    )
    return row is not None


def get_expedition_log(log_id: int) -> AdventurerS1ExpeditionLog | None:
    return (
        db.session.query(AdventurerS1ExpeditionLog)
        .filter(AdventurerS1ExpeditionLog.id == int(log_id))
        .first()
    )


def get_building_usage_log(log_id: int) -> AdventurerS1BuildingUsageLog | None:
    return (
        db.session.query(AdventurerS1BuildingUsageLog)
        .options(joinedload(AdventurerS1BuildingUsageLog.adventurer))
        .filter(AdventurerS1BuildingUsageLog.id == int(log_id))
        .first()
    )


def get_latest_success_token_usage(
    *,
    call_type: str,
    expedition_log_id: int | None = None,
    correlation_id: str | None = None,
) -> AdventurerS1TokenUsage | None:
    query = db.session.query(AdventurerS1TokenUsage).filter(
        AdventurerS1TokenUsage.call_type == str(call_type),
        AdventurerS1TokenUsage.is_success.is_(True),
    )
    if expedition_log_id is not None:
        query = query.filter(
            AdventurerS1TokenUsage.expedition_log_id == int(expedition_log_id),
        )
    elif correlation_id:
        query = query.filter(
            AdventurerS1TokenUsage.correlation_id == str(correlation_id),
        )
    else:
        return None
    return query.order_by(AdventurerS1TokenUsage.id.desc()).first()


def get_feedback(
    user_id: int,
    target_type: str,
    target_id: int,
) -> AdventurerS1AiFeedback | None:
    return (
        db.session.query(AdventurerS1AiFeedback)
        .filter(
            AdventurerS1AiFeedback.user_id == int(user_id),
            AdventurerS1AiFeedback.target_type == str(target_type),
            AdventurerS1AiFeedback.target_id == int(target_id),
        )
        .first()
    )


def get_feedback_map(
    user_id: int,
    target_keys: Iterable[tuple[str, int]],
) -> dict[tuple[str, int], AdventurerS1AiFeedback]:
    keys = {
        (str(target_type), int(target_id))
        for target_type, target_id in target_keys
    }
    if not keys:
        return {}
    rows = (
        db.session.query(AdventurerS1AiFeedback)
        .filter(
            AdventurerS1AiFeedback.user_id == int(user_id),
            tuple_(
                AdventurerS1AiFeedback.target_type,
                AdventurerS1AiFeedback.target_id,
            ).in_(list(keys)),
        )
        .all()
    )
    return {
        (str(row.target_type), int(row.target_id)): row
        for row in rows
    }


def upsert_feedback(
    *,
    user_id: int,
    target_type: str,
    target_id: int,
    quality_score: int,
    preference_score: int,
    token_usage: AdventurerS1TokenUsage,
) -> AdventurerS1AiFeedback:
    feedback = get_feedback(user_id, target_type, target_id)
    if feedback is None:
        feedback = AdventurerS1AiFeedback(
            user_id=int(user_id),
            target_type=str(target_type),
            target_id=int(target_id),
        )
        db.session.add(feedback)

    feedback.quality_score = int(quality_score)
    feedback.preference_score = int(preference_score)
    feedback.token_usage_id = int(token_usage.id)
    feedback.call_type = str(token_usage.call_type)
    feedback.model = token_usage.model
    feedback.profile_id = token_usage.profile_id
    feedback.provider = token_usage.provider
    feedback.endpoint_host = token_usage.endpoint_host
    feedback.api_key_slot = token_usage.api_key_slot
    feedback.api_endpoint_slot = token_usage.api_endpoint_slot
    feedback.provider_request_id = token_usage.provider_request_id
    feedback.updated_at = _portable_utc_now()
    db.session.flush()
    return feedback


def _admin_filters(
    since: datetime | None,
    target_type: str | None,
) -> list:
    filters = []
    if since is not None:
        filters.append(AdventurerS1AiFeedback.updated_at >= since)
    if target_type:
        filters.append(AdventurerS1AiFeedback.target_type == target_type)
    return filters


def get_admin_summary(
    *,
    since: datetime | None,
    target_type: str | None,
) -> tuple[int, int, float | None, float | None]:
    row = (
        db.session.query(
            func.count(AdventurerS1AiFeedback.id),
            func.count(func.distinct(AdventurerS1AiFeedback.user_id)),
            func.avg(AdventurerS1AiFeedback.quality_score),
            func.avg(AdventurerS1AiFeedback.preference_score),
        )
        .filter(*_admin_filters(since, target_type))
        .one()
    )
    return int(row[0] or 0), int(row[1] or 0), row[2], row[3]


def get_admin_target_breakdown(
    *,
    since: datetime | None,
    target_type: str | None,
) -> list[tuple]:
    return (
        db.session.query(
            AdventurerS1AiFeedback.target_type,
            func.count(AdventurerS1AiFeedback.id),
            func.avg(AdventurerS1AiFeedback.quality_score),
            func.avg(AdventurerS1AiFeedback.preference_score),
        )
        .filter(*_admin_filters(since, target_type))
        .group_by(AdventurerS1AiFeedback.target_type)
        .order_by(func.count(AdventurerS1AiFeedback.id).desc())
        .all()
    )


def get_admin_route_breakdown(
    *,
    since: datetime | None,
    target_type: str | None,
) -> list[tuple]:
    return (
        db.session.query(
            AdventurerS1AiFeedback.call_type,
            AdventurerS1AiFeedback.model,
            AdventurerS1AiFeedback.profile_id,
            AdventurerS1AiFeedback.provider,
            AdventurerS1AiFeedback.endpoint_host,
            AdventurerS1AiFeedback.api_key_slot,
            AdventurerS1AiFeedback.api_endpoint_slot,
            func.count(AdventurerS1AiFeedback.id),
            func.avg(AdventurerS1AiFeedback.quality_score),
            func.avg(AdventurerS1AiFeedback.preference_score),
        )
        .filter(*_admin_filters(since, target_type))
        .group_by(
            AdventurerS1AiFeedback.call_type,
            AdventurerS1AiFeedback.model,
            AdventurerS1AiFeedback.profile_id,
            AdventurerS1AiFeedback.provider,
            AdventurerS1AiFeedback.endpoint_host,
            AdventurerS1AiFeedback.api_key_slot,
            AdventurerS1AiFeedback.api_endpoint_slot,
        )
        .order_by(func.count(AdventurerS1AiFeedback.id).desc())
        .all()
    )


def get_admin_recent_feedback(
    *,
    since: datetime | None,
    target_type: str | None,
    page: int,
    per_page: int,
) -> tuple[list[tuple[AdventurerS1AiFeedback, User | None]], int]:
    filters = _admin_filters(since, target_type)
    total = int(
        db.session.query(func.count(AdventurerS1AiFeedback.id))
        .filter(*filters)
        .scalar()
        or 0
    )
    rows = (
        db.session.query(AdventurerS1AiFeedback, User)
        .outerjoin(User, User.id == AdventurerS1AiFeedback.user_id)
        .filter(*filters)
        .order_by(
            AdventurerS1AiFeedback.updated_at.desc(),
            AdventurerS1AiFeedback.id.desc(),
        )
        .offset((int(page) - 1) * int(per_page))
        .limit(int(per_page))
        .all()
    )
    return rows, total
