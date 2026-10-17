"""Serialize S1 role admission, archival and reconciliation per database."""

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
import threading

from flask import current_app
from greenlet import getcurrent

from db_repository.postgresql_advisory_lock import acquire_postgresql_advisory_lock
from extensions import db


logger = logging.getLogger(__name__)

# Keep the existing draw lock key so older draw workers still contend with us.
ROLE_ADMISSION_ADVISORY_LOCK_KEY = 910630152
_local_lock = threading.Lock()


@dataclass
class _AdmissionLock:
    engine: object
    owner: object
    handle: object | None
    depth: int = 1
    context_token: Token | None = None


_held_lock: ContextVar[_AdmissionLock | None] = ContextVar(
    's1_role_admission_lock', default=None,
)


def try_acquire_s1_role_admission_lock() -> _AdmissionLock | None:
    """Borrow only this execution's lock, otherwise acquire the database lock.

    A draw can restore an archived role or reconcile a rejected central claim.
    Those nested calls retain the outer lock through local commit and central
    confirmation. Copied contexts in another greenlet cannot borrow ownership.
    """
    engine = db.engine
    owner = getcurrent()
    held = _held_lock.get()
    if held is not None and held.engine is engine and held.owner is owner:
        held.depth += 1
        return held

    db_uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI') or '')
    handle = None
    if 'postgresql' not in db_uri:
        if not _local_lock.acquire(blocking=False):
            return None
    else:
        try:
            # This dedicated AUTOCOMMIT connection is the control lock itself;
            # it never retains an ORM transaction or a business-pool connection.
            handle = acquire_postgresql_advisory_lock(
                engine.url,
                ROLE_ADMISSION_ADVISORY_LOCK_KEY,
                engine_options=current_app.config.get('SQLALCHEMY_ENGINE_OPTIONS'),
            )
        except Exception:
            logger.warning('S1 角色创建/对账锁获取失败', exc_info=True)
            return None
        if handle is None:
            return None

    held = _AdmissionLock(engine=engine, owner=owner, handle=handle)
    held.context_token = _held_lock.set(held)
    return held


def release_s1_role_admission_lock(held: _AdmissionLock) -> None:
    """Only the outermost owner releases the physical database lock."""
    if _held_lock.get() is not held or held.owner is not getcurrent():
        raise RuntimeError('不能释放其他执行上下文的 S1 角色创建/对账锁')
    held.depth -= 1
    if held.depth:
        return
    try:
        if held.handle is None:
            _local_lock.release()
        elif not held.handle.release():
            raise RuntimeError('当前连接未持有 S1 角色创建/对账锁')
    except Exception:
        logger.warning('S1 角色创建/对账锁释放失败', exc_info=True)
    finally:
        _held_lock.reset(held.context_token)
