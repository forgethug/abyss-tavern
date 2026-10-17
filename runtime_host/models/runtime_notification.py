from runtime_core.time import utc_now
from datetime import datetime

from extensions import db


class RuntimeNotification(db.Model):
    __tablename__ = "runtime_notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    sender_id = db.Column(db.Integer, nullable=True)
    sender_name = db.Column(db.String(100), nullable=True)
    mail_type = db.Column(db.String(30), nullable=True)
    reference_id = db.Column(db.Integer, nullable=True, index=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "content": self.content,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "mail_type": self.mail_type,
            "reference_id": self.reference_id,
            "is_read": self.is_read,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at
                else None
            ),
        }
