from runtime_core.time import utc_now
from runtime_core.time import utc_from_timestamp
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import current_app, g, request

from db_repository import db_manager
from models import User
from utils.responses import api_response


def generate_token(user_id: int) -> str:
    now = utc_now()
    payload = {
        "user_id": user_id,
        "iat": now,
        "exp": now + timedelta(hours=current_app.config["JWT_EXPIRES_HOURS"]),
    }
    return jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm="HS256")


def login_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return api_response(401, "未登录")

        try:
            payload = jwt.decode(
                auth_header.removeprefix("Bearer ").strip(),
                current_app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )
            user = db_manager.query(User).filter_by(id=payload["user_id"]).first()
            if not user or not user.is_active:
                return api_response(401, "用户不存在或已停用")
            if user.token_invalidated_at:
                issued_at = utc_from_timestamp(payload["iat"])
                if issued_at < user.token_invalidated_at:
                    return api_response(401, "登录已失效，请重新登录")
            g.current_user = user
        except jwt.ExpiredSignatureError:
            return api_response(401, "Token已过期")
        except (jwt.InvalidTokenError, KeyError, TypeError):
            return api_response(401, "Token无效")

        return view(*args, **kwargs)

    return decorated


def admin_required(view):
    @wraps(view)
    @login_required
    def decorated(*args, **kwargs):
        if not g.current_user.is_admin:
            return api_response(403, "需要管理员权限")
        return view(*args, **kwargs)

    return decorated

