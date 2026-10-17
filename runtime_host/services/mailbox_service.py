from db_repository import db_manager
from models.runtime_notification import RuntimeNotification


class MailboxService:
    @staticmethod
    def send_mail(
        user_id,
        title,
        content,
        sender_id=None,
        sender_name=None,
        mail_type=None,
        reference_id=None,
        auto_commit=True,
        **_unused,
    ):
        notification = RuntimeNotification(
            user_id=int(user_id),
            title=str(title),
            content=str(content),
            sender_id=int(sender_id) if sender_id is not None else None,
            sender_name=sender_name,
            mail_type=mail_type,
            reference_id=reference_id,
        )
        ok, error = db_manager.add(notification)
        if not ok:
            return None, error
        if auto_commit:
            ok, error = db_manager.commit()
            if not ok:
                return None, error
        return notification.to_dict(), None
