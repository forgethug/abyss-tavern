from runtime_core.time import utc_now
from datetime import datetime

from extensions import db


class Tavern(db.Model):
    __tablename__ = "taverns"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slug = db.Column(db.String(48), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    game_pack = db.Column(db.String(64), nullable=False)
    game_pack_release = db.Column(db.String(64), nullable=False)
    runtime_mode = db.Column(db.String(32), nullable=False)
    runtime_namespace = db.Column(db.String(36), unique=True, nullable=False)
    runtime_port = db.Column(db.Integer, unique=True, nullable=True)
    runtime_status = db.Column(db.String(20), nullable=False, default="pending")
    runtime_pid = db.Column(db.Integer, nullable=True)
    runtime_started_at = db.Column(db.DateTime, nullable=True)
    runtime_error = db.Column(db.String(500), nullable=True)
    created_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    memberships = db.relationship(
        "TavernMembership",
        back_populates="tavern",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    access_policy = db.relationship(
        "TavernAccessPolicy",
        back_populates="tavern",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )

    def to_dict(
        self,
        role: str = None,
        *,
        can_self_join: bool = False,
    ) -> dict:
        policy = self.access_policy
        result = {
            "id": self.id,
            "slug": self.slug,
            "display_name": self.display_name,
            "status": self.status,
            "game_pack": self.game_pack,
            "game_pack_release": self.game_pack_release,
            "is_public": bool(policy and policy.is_public),
            "allow_self_join": bool(policy and policy.allow_self_join),
            "can_self_join": bool(can_self_join),
            "membership_role": role,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        return result

    def to_admin_dict(self) -> dict:
        result = self.to_dict()
        result.update(
            {
                "runtime_mode": self.runtime_mode,
                "runtime_namespace": self.runtime_namespace,
                "runtime_port": self.runtime_port,
                "runtime_status": self.runtime_status,
                "runtime_pid": self.runtime_pid,
                "runtime_started_at": (
                    self.runtime_started_at.isoformat()
                    if self.runtime_started_at
                    else None
                ),
                "runtime_error": self.runtime_error,
                "created_by_user_id": self.created_by_user_id,
            }
        )
        return result


class TavernAccessPolicy(db.Model):
    __tablename__ = "tavern_access_policies"

    tavern_id = db.Column(
        db.Integer,
        db.ForeignKey("taverns.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_public = db.Column(db.Boolean, nullable=False, default=False)
    allow_self_join = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    tavern = db.relationship("Tavern", back_populates="access_policy")


class TavernMembership(db.Model):
    __tablename__ = "tavern_memberships"
    __table_args__ = (
        db.UniqueConstraint("tavern_id", "user_id", name="uq_tavern_membership"),
        db.Index("idx_tavern_membership_user_status", "user_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tavern_id = db.Column(
        db.Integer,
        db.ForeignKey("taverns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(20), nullable=False, default="member")
    status = db.Column(db.String(20), nullable=False, default="active")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    tavern = db.relationship("Tavern", back_populates="memberships")
    user = db.relationship("User")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tavern_id": self.tavern_id,
            "user_id": self.user_id,
            "role": self.role,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
