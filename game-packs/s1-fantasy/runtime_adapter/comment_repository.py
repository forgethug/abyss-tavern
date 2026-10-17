"""S1 comment repository with the platform's minimal local user projection."""

from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from extensions import db
from models.user import User
from plugins.adventurer_s1.models import AdventurerS1ExpeditionComment


def get_comment_counts(expedition_log_id: int) -> dict[tuple[str, int], int]:
    rows = (
        db.session.query(
            AdventurerS1ExpeditionComment.target_type,
            AdventurerS1ExpeditionComment.target_index,
            func.count(),
        )
        .filter(
            AdventurerS1ExpeditionComment.expedition_log_id == int(expedition_log_id),
            AdventurerS1ExpeditionComment.is_deleted.is_(False),
        )
        .group_by(
            AdventurerS1ExpeditionComment.target_type,
            AdventurerS1ExpeditionComment.target_index,
        )
        .all()
    )
    return {
        (str(target_type), int(target_index)): int(count)
        for target_type, target_index, count in rows
    }


def list_comments(
    expedition_log_id: int,
    target_type: str,
    target_index: int,
    *,
    page: int,
    per_page: int,
) -> tuple[list[AdventurerS1ExpeditionComment], int]:
    filters: Iterable = (
        AdventurerS1ExpeditionComment.expedition_log_id == int(expedition_log_id),
        AdventurerS1ExpeditionComment.target_type == target_type,
        AdventurerS1ExpeditionComment.target_index == int(target_index),
        AdventurerS1ExpeditionComment.is_deleted.is_(False),
    )
    total = db.session.query(func.count()).filter(*filters).scalar() or 0
    items = (
        db.session.query(AdventurerS1ExpeditionComment)
        .options(
            joinedload(AdventurerS1ExpeditionComment.user).load_only(
                User.id,
                User.username,
                User.nickname,
            )
        )
        .filter(*filters)
        .order_by(
            AdventurerS1ExpeditionComment.created_at.desc(),
            AdventurerS1ExpeditionComment.id.desc(),
        )
        .offset((int(page) - 1) * int(per_page))
        .limit(int(per_page))
        .all()
    )
    return items, int(total)


def get_latest_comment_for_user(user_id: int) -> AdventurerS1ExpeditionComment | None:
    return (
        db.session.query(AdventurerS1ExpeditionComment)
        .filter(AdventurerS1ExpeditionComment.user_id == int(user_id))
        .order_by(
            AdventurerS1ExpeditionComment.created_at.desc(),
            AdventurerS1ExpeditionComment.id.desc(),
        )
        .first()
    )


def lock_comment_author(user_id: int) -> bool:
    row = (
        db.session.query(User.id)
        .filter(User.id == int(user_id))
        .with_for_update()
        .first()
    )
    return row is not None


def create_comment(
    expedition_log_id: int,
    target_type: str,
    target_index: int,
    user_id: int,
    content: str,
) -> AdventurerS1ExpeditionComment:
    comment = AdventurerS1ExpeditionComment(
        expedition_log_id=int(expedition_log_id),
        target_type=target_type,
        target_index=int(target_index),
        user_id=int(user_id),
        content=content,
    )
    db.session.add(comment)
    db.session.flush()
    return comment


def get_comment(comment_id: int) -> AdventurerS1ExpeditionComment | None:
    return (
        db.session.query(AdventurerS1ExpeditionComment)
        .options(
            joinedload(AdventurerS1ExpeditionComment.user).load_only(
                User.id,
                User.username,
                User.nickname,
            )
        )
        .filter(AdventurerS1ExpeditionComment.id == int(comment_id))
        .first()
    )


def count_comments_for_target(
    expedition_log_id: int,
    target_type: str,
    target_index: int,
) -> int:
    return int(
        db.session.query(func.count())
        .filter(
            AdventurerS1ExpeditionComment.expedition_log_id == int(expedition_log_id),
            AdventurerS1ExpeditionComment.target_type == target_type,
            AdventurerS1ExpeditionComment.target_index == int(target_index),
            AdventurerS1ExpeditionComment.is_deleted.is_(False),
        )
        .scalar()
        or 0
    )
