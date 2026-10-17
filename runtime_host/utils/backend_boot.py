"""Stable identity for one isolated Runtime host process start."""

import os
import socket
from uuid import uuid4


def _build_backend_boot_id() -> str:
    configured = (os.environ.get("BACKEND_BOOT_ID") or "").strip()
    if configured:
        return configured[:128]
    release_id = (os.environ.get("BACKEND_RELEASE_ID") or "").strip()[:64]
    instance_id = uuid4().hex
    prefix = f"{release_id}:" if release_id else "local:"
    return f"{prefix}{socket.gethostname()}:{os.getpid()}:{instance_id}"[:128]


_BACKEND_BOOT_ID = _build_backend_boot_id()


def get_backend_boot_id() -> str:
    return _BACKEND_BOOT_ID


def get_async_worker_boot_id() -> str:
    # Portable taverns embed their one AI queue in the dedicated host process.
    # The producer-only, shared Redis worker used by gupiao is not involved.
    return get_backend_boot_id()
