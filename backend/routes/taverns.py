from urllib.error import URLError

from flask import Blueprint, Response, g, request

from services.runtime_supervisor_service import RuntimeSupervisorService
from services.tavern_service import TavernService
from utils.auth import login_required
from utils.responses import api_response


taverns_bp = Blueprint("taverns", __name__, url_prefix="/api/taverns")


@taverns_bp.get("")
@login_required
def list_taverns():
    return api_response(200, "success", TavernService.list_for_user(g.current_user))


@taverns_bp.post("/<string:slug>/join")
@login_required
def join_public_tavern(slug: str):
    result, error = TavernService.join_public(g.current_user, slug)
    if error:
        status = 404 if error == "酒馆不存在或未开放" else 403
        return api_response(status, error)
    return api_response(200, "已加入酒馆", result)


@taverns_bp.get("/<string:slug>/context")
@login_required
def runtime_context(slug: str):
    result, error = TavernService.get_runtime_context(g.current_user, slug)
    if error:
        if error == "你不是这个酒馆的成员":
            status = 403
        elif error == "酒馆不存在或未开放":
            status = 404
        else:
            status = 503
        return api_response(status, error)
    return api_response(200, "success", result)


@taverns_bp.route(
    "/<string:slug>/runtime",
    defaults={"runtime_path": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@taverns_bp.route(
    "/<string:slug>/runtime/<path:runtime_path>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
@login_required
def runtime_proxy(slug: str, runtime_path: str):
    access, error = TavernService.resolve_access(g.current_user, slug)
    if error:
        status = 403 if error == "你不是这个酒馆的成员" else 404
        return api_response(status, error)
    tavern, _ = access

    if runtime_path.split('/', 1)[0] == 'admin' and not g.current_user.is_admin:
        return api_response(403, '需要管理员权限')

    ready, runtime_error = RuntimeSupervisorService.ensure_ready(
        tavern,
        g.current_user,
    )
    if not ready:
        return api_response(503, runtime_error or "酒馆 Runtime 启动失败")

    try:
        body, status, response_headers = RuntimeSupervisorService.proxy_request(
            tavern,
            runtime_path=runtime_path,
            query_string=request.query_string,
            method=request.method,
            body=request.get_data(cache=False),
            headers=request.headers,
        )
    except (URLError, TimeoutError, OSError):
        return api_response(502, "酒馆 Runtime 暂时不可用")
    return Response(body, status=status, headers=response_headers)
