from flask import Blueprint, g, request

from services.auth_service import AuthService
from utils.auth import login_required
from utils.responses import api_response


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.post("/register")
def register():
    payload = request.get_json(silent=True) or {}
    result, error = AuthService.register(
        username=payload.get("username"),
        password=payload.get("password"),
        display_name=payload.get("display_name"),
    )
    if error:
        status = 409 if error == "用户名已存在" else 400
        return api_response(status, error)
    return api_response(201, "注册成功", result)


@auth_bp.post("/login")
def login():
    payload = request.get_json(silent=True) or {}
    result, error = AuthService.login(
        username=payload.get("username"),
        password=payload.get("password"),
    )
    if error:
        return api_response(401, error)
    return api_response(200, "登录成功", result)


@auth_bp.get("/me")
@login_required
def me():
    return api_response(200, "success", g.current_user.to_dict())

