"""Lossless representation normalization for S1 AI structured outputs.

These helpers only bridge representation drift that has one exact JSON meaning.
They never provide defaults or replace downstream identity, range, authority, or
cross-field validation.
"""
from __future__ import annotations

from collections.abc import Collection
from decimal import Decimal, InvalidOperation
import math
import re


_ASCII_DECIMAL_PATTERN = re.compile(r'-?\d+(?:\.\d+)?', flags=re.ASCII)
_MAX_ASCII_INTEGER_DIGITS = 128
_MAX_ASCII_NUMBER_CHARS = 128


def normalize_exact_boolean(value: object) -> tuple[object, bool]:
    """Return an exact JSON boolean spelling encoded as an ASCII string."""
    if type(value) is bool:
        return value, False
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if not stripped.isascii():
        return value, False
    lowered = stripped.lower()
    if lowered == 'true':
        return True, True
    if lowered == 'false':
        return False, True
    return value, False


def normalize_exact_catalog_reference_object(
    value: object,
    *,
    reference_id_field: str,
    allowed_reference_ids: Collection[str],
) -> tuple[object, bool]:
    """Wrap one exact backend-issued catalog ID in its one-field object.

    Existing objects remain untouched for their owning validator to project.
    Unknown IDs and all other types also remain untouched so the strict caller
    retains its normal error and cannot gain identity or authority here.
    """
    if isinstance(value, dict) or not isinstance(value, str):
        return value, False
    if value not in allowed_reference_ids:
        return value, False
    return {reference_id_field: value}, True


def normalize_ascii_integer(
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[object, bool]:
    """Return a complete unsigned ASCII decimal string as an integer.

    Signs, decimal points, exponents, non-ASCII digits, booleans, floats, and
    pathologically long values stay untouched for the ordinary strict validator.
    Optional bounds only decide whether the representation may be normalized;
    callers must still run their authoritative range checks afterwards.
    """
    if type(value) is int:
        return value, False
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > _MAX_ASCII_INTEGER_DIGITS
        or not stripped.isascii()
        or not stripped.isdecimal()
    ):
        return value, False
    try:
        normalized = int(stripped)
    except ValueError:
        return value, False
    if minimum is not None and normalized < minimum:
        return value, False
    if maximum is not None and normalized > maximum:
        return value, False
    return normalized, True


def normalize_ascii_finite_number(value: object) -> tuple[object, bool]:
    """Return a plain finite ASCII decimal string as a Python float.

    Exponents, a leading plus sign, commas, symbolic values, and non-ASCII
    digits stay untouched. Business-specific bounds remain the caller's job.
    """
    if not isinstance(value, str):
        return value, False
    stripped = value.strip()
    if (
        len(stripped) > _MAX_ASCII_NUMBER_CHARS
        or not stripped.isascii()
        or _ASCII_DECIMAL_PATTERN.fullmatch(stripped) is None
    ):
        return value, False
    try:
        decimal_value = Decimal(stripped)
        normalized = float(decimal_value)
    except (InvalidOperation, OverflowError, ValueError):
        return value, False
    if not decimal_value.is_finite() or not math.isfinite(normalized):
        return value, False
    if Decimal(str(normalized)) != decimal_value:
        return value, False
    return normalized, True
