from functools import wraps

from flask import jsonify, request


def admin_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        user = getattr(request, "current_user", None)
        if not user:
            return jsonify({"code": 401, "message": "未登录", "data": None}), 401
        if not user.is_admin:
            return jsonify(
                {"code": 403, "message": "需要管理员权限", "data": None}
            ), 403
        return view(*args, **kwargs)

    return decorated


def admin_read_required(view):
    """Map S1 read-admin routes to the platform's central admin role."""

    return admin_required(view)


def admin_primary_required(view):
    """Map S1 primary-admin routes to the platform's central admin role."""

    return admin_required(view)


def generic_anti_cheat_check(_category):
    """Keep S1 route contracts while the control plane owns edge throttling."""

    def decorator(view):
        @wraps(view)
        def decorated(*args, **kwargs):
            return view(*args, **kwargs)

        return decorated

    return decorator
