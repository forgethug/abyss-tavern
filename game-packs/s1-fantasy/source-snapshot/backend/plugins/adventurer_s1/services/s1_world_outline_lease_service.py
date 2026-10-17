"""Keep maintenance ownership alive without holding a connection during AI."""
import logging
import threading
from contextlib import contextmanager

from flask import current_app

from db_repository import db_manager
from db_repository.s1_world_outline_lease_repository import renew_maintenance_claim

logger = logging.getLogger(__name__)


def maintenance_lease_settings() -> tuple[int, int]:
    heartbeat_seconds = max(1, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_OUTLINE_HEARTBEAT_SECONDS', 30,
    )))
    lease_seconds = max(heartbeat_seconds * 3, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_OUTLINE_LEASE_SECONDS', 120,
    )))
    return heartbeat_seconds, lease_seconds


@contextmanager
def maintenance_heartbeat(*, heartbeat_seconds: int, **claim):
    app = current_app._get_current_object()
    stop = threading.Event()

    def run():
        with app.app_context():
            while not stop.wait(heartbeat_seconds):
                try:
                    if not renew_maintenance_claim(**claim):
                        return
                except Exception:
                    logger.exception(
                        'S1 世界维护租约续期失败 dungeon=%s', claim['dungeon'],
                    )
                finally:
                    db_manager.remove()

    worker = threading.Thread(
        target=run, daemon=True, name='s1-world-outline-heartbeat',
    )
    worker.start()
    try:
        yield
    finally:
        # Any in-flight DB renewal is token-fenced and uses its own app session.
        # Never wait for it while the consumer may need the same aggregate lock.
        stop.set()
