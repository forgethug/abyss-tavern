from urllib.error import URLError

from flask import Blueprint, Response, g, request

from db_repository import db_manager
from models import User
from services.tavern_service import TavernService
from services.game_pack_registry import GamePackRegistry
from services.runtime_supervisor_service import RuntimeSupervisorService
from utils.auth import admin_required
from utils.responses import api_response


admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


@admin_bp.get("/game-packs")
@admin_required
def list_game_packs():
    return api_response(200, "success", GamePackRegistry.public_list())


@admin_bp.get("/users")
@admin_required
def list_users():
    users = db_manager.query(User).order_by(User.id.asc()).all()
    return api_response(200, "success", [user.to_dict() for user in users])


@admin_bp.put("/users/<int:user_id>/status")
@admin_required
def update_user_status(user_id: int):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("is_active"), bool):
        return api_response(400, "is_active必须是布尔值")
    result, error = TavernService.set_user_active(
        actor=g.current_user,
        user_id=user_id,
        is_active=payload["is_active"],
    )
    if error:
        return api_response(400, error)
    return api_response(200, "用户状态已更新", result)


@admin_bp.post("/taverns")
@admin_required
def create_tavern():
    payload = request.get_json(silent=True) or {}
    result, error = TavernService.create_tavern(
        creator=g.current_user,
        display_name=payload.get("display_name"),
        slug=payload.get("slug"),
        game_pack=payload.get("game_pack"),
        is_public=payload.get("is_public", False),
        allow_self_join=payload.get("allow_self_join", False),
    )
    if error:
        status = 409 if error == "访问标识已存在" else 400
        return api_response(status, error)
    return api_response(201, "酒馆已开放", result)


@admin_bp.put("/taverns/<int:tavern_id>")
@admin_required
def update_tavern(tavern_id: int):
    payload = request.get_json(silent=True) or {}
    result, error = TavernService.update_tavern(
        tavern_id=tavern_id,
        display_name=payload.get("display_name"),
        status=payload.get("status"),
        is_public=payload.get("is_public"),
        allow_self_join=payload.get("allow_self_join"),
    )
    if error:
        return api_response(400, error)
    return api_response(200, "酒馆已更新", result)


@admin_bp.post("/taverns/<int:tavern_id>/members")
@admin_required
def add_tavern_member(tavern_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        return api_response(400, "user_id无效")
    result, error = TavernService.add_member(
        tavern_id=tavern_id,
        user_id=user_id,
        role=payload.get("role", "member"),
    )
    if error:
        return api_response(400, error)
    return api_response(200, "成员已加入酒馆", result)


@admin_bp.post("/taverns/<int:tavern_id>/runtime/<string:action>")
@admin_required
def control_tavern_runtime(tavern_id: int, action: str):
    result, error = TavernService.control_runtime(
        actor=g.current_user,
        tavern_id=tavern_id,
        action=action,
    )
    if error:
        if error == "酒馆不存在":
            return api_response(404, error)
        if error == "Runtime 操作无效":
            return api_response(400, error)
        return api_response(409, error)
    action_messages = {
        "start": "酒馆 Runtime 已启动",
        "stop": "酒馆 Runtime 已停止",
        "restart": "酒馆 Runtime 已重启",
    }
    return api_response(200, action_messages[action], result)


@admin_bp.route(
    "/taverns/<int:tavern_id>/narrative",
    defaults={"narrative_path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@admin_bp.route(
    "/taverns/<int:tavern_id>/narrative/<path:narrative_path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@admin_required
def narrative_runtime_proxy(tavern_id: int, narrative_path: str):
    tavern = TavernService.get_by_id(tavern_id)
    if tavern is None:
        return api_response(404, "酒馆不存在")
    if tavern.status != "active":
        return api_response(409, "酒馆未开放")

    ready, runtime_error = RuntimeSupervisorService.ensure_ready(
        tavern,
        g.current_user,
    )
    if not ready:
        return api_response(
            503,
            runtime_error or "酒馆 Runtime 启动失败",
        )
    try:
        body, status, response_headers = (
            RuntimeSupervisorService.proxy_narrative_request(
                tavern,
                narrative_path=narrative_path,
                query_string=request.query_string,
                method=request.method,
                body=request.get_data(cache=False),
                headers=request.headers,
            )
        )
    except (URLError, TimeoutError, OSError):
        return api_response(502, "酒馆 Runtime 暂时不可用")
    return Response(body, status=status, headers=response_headers)
