from flask import request


def get_pagination_params(default_page=1, default_per_page=20, max_per_page=100):
    page = max(request.args.get("page", default_page, type=int), 1)
    per_page = request.args.get("per_page", default_per_page, type=int)
    return page, min(max(per_page, 1), max_per_page)


def build_paginated_response(items, total, page, per_page):
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "has_next": page * per_page < total,
        "has_prev": page > 1,
    }
