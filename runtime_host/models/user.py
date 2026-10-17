from runtime_core.time import utc_now
from datetime import datetime

from extensions import db


class User(db.Model):
    """Runtime-local identity projection; contains no stock-platform fields."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    nickname = db.Column(db.String(64), nullable=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    @property
    def active_title(self):
        """Keep upstream comment serialization compatible without storing it."""

        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "nickname": self.nickname or self.username,
            "is_admin": self.is_admin,
            "is_deleted": self.is_deleted,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
