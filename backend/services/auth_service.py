from runtime_core.time import utc_now
import re
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from db_repository import db_manager
from models import User
from utils.auth import generate_token


class AuthService:
    @staticmethod
    def register(
        username: str,
        password: str,
        display_name: Optional[str] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        username = (username or "").strip()
        display_name = (display_name or "").strip() or None

        min_length = current_app.config["USERNAME_MIN_LENGTH"]
        max_length = current_app.config["USERNAME_MAX_LENGTH"]
        if not min_length <= len(username) <= max_length:
            return None, f"用户名长度需为{min_length}-{max_length}字符"
        if not re.fullmatch(current_app.config["USERNAME_PATTERN"], username):
            return None, "用户名只能包含字母、数字和下划线"

        password_min = current_app.config["PASSWORD_MIN_LENGTH"]
        password_max = current_app.config["PASSWORD_MAX_LENGTH"]
        if not password_min <= len(password or "") <= password_max:
            return None, f"密码长度需为{password_min}-{password_max}字符"

        if display_name and len(display_name) > current_app.config["DISPLAY_NAME_MAX_LENGTH"]:
            return None, "显示名称过长"
        if db_manager.query(User).filter_by(username=username).first():
            return None, "用户名已存在"

        user = User(
            username=username,
            display_name=display_name,
            password_hash=generate_password_hash(password),
        )
        ok, error = db_manager.add(user)
        if not ok:
            return None, error
        ok, error = db_manager.commit()
        if not ok:
            if "unique" in (error or "").lower():
                return None, "用户名已存在"
            return None, "注册失败，请稍后重试"

        return {"token": generate_token(user.id), "user": user.to_public_dict()}, None

    @staticmethod
    def login(username: str, password: str) -> Tuple[Optional[dict], Optional[str]]:
        user = db_manager.query(User).filter_by(username=(username or "").strip()).first()
        if not user or not check_password_hash(user.password_hash, password or ""):
            return None, "用户名或密码错误"
        if not user.is_active:
            return None, "账号已停用"

        user.last_login_at = utc_now()
        ok, error = db_manager.commit()
        if not ok:
            return None, error
        return {"token": generate_token(user.id), "user": user.to_public_dict()}, None

