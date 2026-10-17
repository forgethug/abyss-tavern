"""Transparent, lossless storage compression for S1 cold text/JSON fields."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import JSON, Text
from sqlalchemy.types import TypeDecorator


_JSON_MARKER = "__adventurer_s1_zlib_v1__"
_TEXT_PREFIX = "__adventurer_s1_zlib_v1__:"


@dataclass(frozen=True)
class ColdValue:
    """Marks a value for compression on the next database write."""

    value: Any


def mark_cold(value: Any) -> ColdValue:
    return ColdValue(value)


def _compress_bytes(raw: bytes) -> tuple[str, str]:
    checksum = hashlib.sha256(raw).hexdigest()
    payload = base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")
    return checksum, payload


def _decompress_bytes(checksum: str, payload: str) -> bytes:
    raw = zlib.decompress(base64.b64decode(payload.encode("ascii"), validate=True))
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("S1 cold payload checksum mismatch")
    return raw


def encode_cold_json(value: Any) -> dict[str, str]:
    """Return the exact versioned JSON envelope used for a cold database write."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    checksum, payload = _compress_bytes(raw)
    return {_JSON_MARKER: payload, "sha256": checksum}


def estimate_json_storage_bytes(value: Any, *, cold: bool, expedition_log: bool = False) -> int:
    """Estimate serialized logical bytes without claiming PostgreSQL physical size."""
    if cold and expedition_log:
        stored = ColdExpeditionLogJSON().process_bind_param(mark_cold(value), None)
    else:
        stored = encode_cold_json(value) if cold else value
    return len(
        json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


class ColdCompressedJSON(TypeDecorator):
    """JSON column that stores marked cold values as a versioned zlib envelope."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> Any:
        if not isinstance(value, ColdValue):
            return value
        return encode_cold_json(value.value)

    def process_result_value(self, value: Any, dialect) -> Any:
        if not isinstance(value, dict) or _JSON_MARKER not in value:
            return value
        raw = _decompress_bytes(str(value.get("sha256") or ""), str(value[_JSON_MARKER]))
        return json.loads(raw.decode("utf-8"))


def cold_player_name_history(full_log: dict) -> dict:
    """Exactly the legacy cold reader's identity fields and fallback order."""
    mirror = full_log.get('cross_realm_mirror_injection') or {}
    return {
        'member_names': full_log.get('_member_id_to_name'),
        'names': [row.get('adventurer_name', row.get('name'))
                  for row in full_log.get('audit_member_snapshot') or []
                  if isinstance(row, dict)],
        'legacy_names': [],
        'guest_names': [row.get('display_name')
                        for row in (full_log.get('waiting_player_npc_injection') or {}).get('candidates') or []
                        if isinstance(row, dict)],
        'mirror_name': (mirror.get('candidate') or {}).get('display_name'),
    }


class ColdExpeditionLogJSON(ColdCompressedJSON):
    """Carry derived names beside the lossless envelope for the DB trigger."""

    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> Any:
        stored = super().process_bind_param(value, dialect)
        if isinstance(value, ColdValue):
            try:
                stored['_player_name_history_v1'] = cold_player_name_history(value.value)
            except (AttributeError, TypeError):
                # A derived read accelerator must not block lossless storage
                # of legacy shapes. Those rows retain the existing read path.
                pass
        return stored


class ColdCompressedText(TypeDecorator):
    """TEXT column that transparently expands marked cold values when read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> Any:
        if not isinstance(value, ColdValue):
            return value
        if value.value is None:
            return None
        raw = str(value.value).encode("utf-8")
        checksum, payload = _compress_bytes(raw)
        return f"{_TEXT_PREFIX}{checksum}:{payload}"

    def process_result_value(self, value: Any, dialect) -> Any:
        if not isinstance(value, str) or not value.startswith(_TEXT_PREFIX):
            return value
        checksum, payload = value[len(_TEXT_PREFIX):].split(":", 1)
        return _decompress_bytes(checksum, payload).decode("utf-8")
