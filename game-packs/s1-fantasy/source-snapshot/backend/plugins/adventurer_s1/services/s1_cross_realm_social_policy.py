"""Shared gateway/runtime boundary for explicitly allowed cross-realm social writes."""

import re


def is_cross_realm_birthday_visit(method: str, path: str) -> bool:
    """Only posting a moment to an existing birthday table is cross-realm writable."""
    return method == 'POST' and re.fullmatch(
        r'/api/adventurer-s1/ceremony-hall/birthday/open-tables/[0-9a-f]{32}/visits',
        path,
    ) is not None
