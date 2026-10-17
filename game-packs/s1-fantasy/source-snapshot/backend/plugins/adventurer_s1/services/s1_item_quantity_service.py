"""Utilities for S1 equipment/warehouse stack quantities."""
from __future__ import annotations

import re
from typing import Any


_COUNT_UNITS = r"个|件|瓶|枚|支|把|份|颗|块|本|张|袋|株|根|条|粒|片"
_CN_DIGITS = r"[零〇一二两三四五六七八九十百千万]+"
_QTY_NUM = rf"(?:[1-9]\d{{0,5}}|{_CN_DIGITS})"
_CN_DIGIT_MAP = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_STACK_SUFFIX_PATTERNS = (
    re.compile(r"^(?P<name>.+?)\s*[×xX＊*]\s*(?P<quantity>[1-9]\d{0,5})\s*$"),
    re.compile(
        rf"^(?P<name>.+?)\s*[（(]\s*(?P<quantity>[1-9]\d{{0,5}})\s*"
        rf"(?:{_COUNT_UNITS})\s*[）)]\s*$"
    ),
    re.compile(rf"^(?P<name>.+?)\s*(?P<quantity>[1-9]\d{{0,5}})\s*(?:{_COUNT_UNITS})\s*$"),
)
_CN_SUFFIX_PATTERNS = (
    re.compile(
        rf"^(?P<name>.+?)\s*[×xX＊*]\s*(?P<quantity>{_QTY_NUM})\s*$"
    ),
    re.compile(
        rf"^(?P<name>.+?)\s*[（(]\s*(?P<quantity>{_QTY_NUM})\s*"
        rf"(?:{_COUNT_UNITS})?\s*[）)]\s*$"
    ),
    re.compile(
        rf"^(?P<name>.+?)\s*(?P<quantity>{_QTY_NUM})\s*(?:{_COUNT_UNITS})\s*$"
    ),
    re.compile(
        rf"^(?P<quantity>{_QTY_NUM})\s*(?:{_COUNT_UNITS})\s*(?P<name>.+)$"
    ),
)


def _resolve_quantity_token(token: str) -> int | None:
    text = str(token or "").strip()
    if not text:
        return None
    if re.fullmatch(r"[1-9]\d{0,5}", text):
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十") and len(text) > 1:
        tail = _resolve_quantity_token(text[1:])
        return 10 + (tail or 0)
    if text.endswith("十") and len(text) > 1:
        return _CN_DIGIT_MAP.get(text[:-1], 1) * 10
    if "十" in text:
        left, right = text.split("十", 1)
        return _CN_DIGIT_MAP.get(left, 1) * 10 + _CN_DIGIT_MAP.get(right, 0)
    if text in _CN_DIGIT_MAP:
        return _CN_DIGIT_MAP[text]
    return None


def coerce_item_quantity(value: Any, default: int = 1) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        quantity = default
    return max(1, min(quantity, 999999))


def split_stack_suffix_with_flag(name: str, quantity: Any = 1) -> tuple[str, int, bool]:
    """Return (base_name, semantic_quantity, name_encoded)."""
    clean_name = str(name or "").strip()
    base_quantity = coerce_item_quantity(quantity, 1)
    for pattern in _STACK_SUFFIX_PATTERNS:
        match = pattern.match(clean_name)
        if not match:
            continue
        base_name = match.group("name").strip()
        if not base_name:
            continue
        suffix_quantity = coerce_item_quantity(match.group("quantity"), 1)
        return base_name, max(base_quantity, suffix_quantity), True
    return clean_name, base_quantity, False


def split_stack_suffix(name: str, quantity: Any = 1) -> tuple[str, int]:
    """Return (base_name, quantity), parsing strict trailing count suffixes."""
    base_name, semantic_quantity, _ = split_stack_suffix_with_flag(name, quantity)
    return base_name, semantic_quantity


def parse_embedded_quantity(text: str) -> tuple[str, int | None, bool]:
    """Parse stack quantity embedded in item name/desc (Arabic or Chinese numerals)."""
    clean = str(text or "").strip()
    if not clean:
        return clean, None, False

    base_name, quantity, encoded = split_stack_suffix_with_flag(clean, 1)
    if encoded:
        return base_name, quantity, True

    for pattern in _CN_SUFFIX_PATTERNS:
        match = pattern.match(clean)
        if not match:
            continue
        parsed_name = str(match.group("name") or "").strip()
        parsed_qty = _resolve_quantity_token(match.group("quantity"))
        if not parsed_name or parsed_qty is None or parsed_qty < 1:
            continue
        return parsed_name, min(parsed_qty, 999999), True

    return clean, None, False


def text_has_embedded_quantity(text: str) -> bool:
    _, _, found = parse_embedded_quantity(text)
    return found


def item_quantity_of(item: Any) -> int:
    return coerce_item_quantity(getattr(item, "quantity", 1), 1)


def normalize_item_instance_name_and_quantity(item: Any) -> bool:
    """Normalize an ORM item in place. Returns True when it changed."""
    base_name, quantity = split_stack_suffix(
        getattr(item, "name", ""),
        getattr(item, "quantity", 1),
    )
    changed = False
    if base_name and base_name != getattr(item, "name", None):
        item.name = base_name
        changed = True
    if quantity != item_quantity_of(item):
        item.quantity = quantity
        changed = True
    return changed
