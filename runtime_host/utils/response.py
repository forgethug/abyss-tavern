from flask import jsonify


def success(data=None, message="success", code=200):
    return jsonify({"code": code, "message": message, "data": data}), code


def created(data=None, message="创建成功"):
    return jsonify({"code": 201, "message": message, "data": data}), 201


def error(message="操作失败", code=400, data=None):
    return jsonify({"code": code, "message": message, "data": data}), code


def not_found(message="资源不存在"):
    return error(message, 404)


def unauthorized(message="未登录"):
    return error(message, 401)


def forbidden(message="权限不足"):
    return error(message, 403)


def handle_result(
    result,
    result_error,
    success_msg="success",
    error_code=400,
    success_code=200,
):
    if result_error:
        return error(result_error, error_code)
    return success(result, success_msg, success_code)
