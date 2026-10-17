from functools import wraps

import jwt
from flask import current_app, jsonify, request

from db_repository import db_manager
from models.user import User


def login_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not token:
            return jsonify({"code": 401, "message": "未登录", "data": None}), 401
        try:
            payload = jwt.decode(
                token,
                current_app.config["JWT_SECRET_KEY"],
                algorithms=["HS256"],
            )
            user = db_manager.query(User).filter_by(id=payload["user_id"]).first()
            if not user or user.is_deleted:
                return jsonify(
                    {"code": 401, "message": "用户不存在或已停用", "data": None}
                ), 401
            request.current_user = user
        except jwt.ExpiredSignatureError:
            return jsonify({"code": 401, "message": "Token已过期", "data": None}), 401
        except (jwt.InvalidTokenError, KeyError, TypeError):
            return jsonify({"code": 401, "message": "Token无效", "data": None}), 401
        return view(*args, **kwargs)

    return decorated
