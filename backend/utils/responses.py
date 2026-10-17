from typing import Any, Tuple

from flask import jsonify


def api_response(
    code: int,
    message: str,
    data: Any = None,
    http_status: int = None,
) -> Tuple[Any, int]:
    return jsonify({"code": code, "message": message, "data": data}), http_status or code

