"""Process-lifetime PostgreSQL lease for the singleton region-2 Runtime."""

from __future__ import annotations

import logging
import math
import threading
import time

from sqlalchemy import text

from db_repository.postgresql_advisory_lock import (
    acquire_postgresql_advisory_lock,
)
from extensions import db


logger = logging.getLogger(__name__)
_LEASE_THREAD_STOP_TIMEOUT_MAX_SECONDS = 10.0


class S1Region2RuntimeProcessLeaseService:
    _LOCK_KEY = 's1-runtime-region2-process-v1'
    _lock = threading.Lock()
    _lease_handle = None
    _connection = None
    _app = None
    _backend_pid = None
    _last_heartbeat_monotonic = None
    _heartbeat_interval_seconds = 5.0
    _heartbeat_timeout_seconds = 30.0
    _stop_event = None
    _heartbeat_thread = None
    _watchdog_thread = None
    _lost = False
    _fail_stop_started = False
    _stopping = False

    @classmethod
    def start(cls, app) -> None:
        interval = float(app.config.get(
            'S1_RUNTIME_PROCESS_LEASE_HEARTBEAT_INTERVAL_SECONDS',
            5.0,
        ))
        timeout = float(app.config.get(
            'S1_RUNTIME_PROCESS_LEASE_HEARTBEAT_TIMEOUT_SECONDS',
            30.0,
        ))
        if (
            not math.isfinite(interval)
            or not math.isfinite(timeout)
            or interval <= 0
            or timeout < interval * 3
        ):
            raise RuntimeError('二区 Runtime 进程租约心跳配置无效')

        with cls._lock, app.app_context():
            if cls._stopping:
                raise RuntimeError('二区 Runtime 进程租约正在停止')
            if (
                cls._connection is not None
                and not cls._connection.closed
                and not getattr(cls._connection, 'invalidated', False)
                and not cls._lost
            ):
                return
            if db.engine.dialect.name != 'postgresql':
                raise RuntimeError('二区 Runtime 进程租约要求 PostgreSQL')
            lease_key = (
                f'{cls._LOCK_KEY}:'
                f"{app.config.get('S1_RUNTIME_DEPLOYMENT_ID')}:"
                f'{db.engine.url.database}'
            )
            lease_handle = acquire_postgresql_advisory_lock(
                db.engine.url,
                lease_key,
                engine_options=app.config.get('SQLALCHEMY_ENGINE_OPTIONS'),
            )
            if lease_handle is None:
                raise RuntimeError('已有二区 Runtime 占用该玩法数据库')
            connection = lease_handle.connection
            try:
                backend_pid = connection.execute(
                    text('SELECT pg_backend_pid()')
                ).scalar()
            except Exception:
                lease_handle.discard()
                raise
            if type(backend_pid) is not int or backend_pid <= 0:
                lease_handle.discard()
                raise RuntimeError('二区 Runtime 进程租约后端无效')

            stop_event = threading.Event()
            cls._lease_handle = lease_handle
            cls._connection = connection
            cls._app = app
            cls._backend_pid = backend_pid
            cls._last_heartbeat_monotonic = time.monotonic()
            cls._heartbeat_interval_seconds = interval
            cls._heartbeat_timeout_seconds = timeout
            cls._stop_event = stop_event
            cls._lost = False
            cls._fail_stop_started = False
            cls._stopping = False
            app.config['S1_RUNTIME_PROCESS_LEASE_LOST'] = False
            heartbeat_thread = threading.Thread(
                target=cls._heartbeat_loop,
                name='s1-region2-process-lease-heartbeat',
                daemon=True,
            )
            watchdog_thread = threading.Thread(
                target=cls._watchdog_loop,
                name='s1-region2-process-lease-watchdog',
                daemon=True,
            )
            cls._heartbeat_thread = heartbeat_thread
            cls._watchdog_thread = watchdog_thread

        heartbeat_thread.start()
        watchdog_thread.start()

    @classmethod
    def _heartbeat_loop(cls) -> None:
        while True:
            with cls._lock:
                stop_event = cls._stop_event
                interval = cls._heartbeat_interval_seconds
            if stop_event is None or stop_event.wait(interval):
                return
            with cls._lock:
                if cls._lost or cls._connection is None:
                    return
                connection = cls._connection
                expected_backend_pid = cls._backend_pid
            try:
                actual_backend_pid = connection.execute(
                    text('SELECT pg_backend_pid()')
                ).scalar()
            except Exception as exc:
                cls._mark_lost(f'heartbeat_error:{type(exc).__name__}')
                return
            if (
                type(actual_backend_pid) is not int
                or actual_backend_pid != expected_backend_pid
                or connection.closed
                or getattr(connection, 'invalidated', False)
            ):
                cls._mark_lost('heartbeat_session_changed')
                return
            with cls._lock:
                if not cls._lost and cls._connection is connection:
                    cls._last_heartbeat_monotonic = time.monotonic()

    @classmethod
    def _watchdog_loop(cls) -> None:
        while True:
            with cls._lock:
                stop_event = cls._stop_event
                interval = cls._heartbeat_interval_seconds
                timeout = cls._heartbeat_timeout_seconds
            check_interval = min(interval, max(timeout / 4.0, 0.01))
            if stop_event is None or stop_event.wait(check_interval):
                return
            with cls._lock:
                if cls._lost or cls._connection is None:
                    return
                last_heartbeat = cls._last_heartbeat_monotonic
            if (
                last_heartbeat is None
                or time.monotonic() - last_heartbeat > timeout
            ):
                cls._mark_lost('heartbeat_timeout')
                return

    @classmethod
    def _mark_lost(cls, reason: str) -> None:
        callback = None
        with cls._lock:
            if cls._lost:
                return
            cls._lost = True
            if cls._stop_event is not None:
                cls._stop_event.set()
            app = cls._app
            if app is not None:
                app.config['S1_RUNTIME_PROCESS_LEASE_LOST'] = True
                if not cls._stopping and not cls._fail_stop_started:
                    callback = app.config.get(
                        'S1_RUNTIME_PROCESS_LEASE_LOST_CALLBACK'
                    )
                    cls._fail_stop_started = True
        logger.critical('二区 Runtime 进程租约丢失: %s', reason)
        if callable(callback):
            try:
                callback()
            except Exception:
                logger.exception('二区 Runtime fail-stop 回调失败')

    @classmethod
    def is_alive(cls) -> bool:
        reason = None
        with cls._lock:
            connection = cls._connection
            last_heartbeat = cls._last_heartbeat_monotonic
            timeout = cls._heartbeat_timeout_seconds
            if cls._stopping or cls._lost or connection is None:
                return False
            if connection.closed or getattr(connection, 'invalidated', False):
                reason = 'connection_invalidated'
            elif (
                last_heartbeat is None
                or time.monotonic() - last_heartbeat > timeout
            ):
                reason = 'heartbeat_stale'
        if reason is not None:
            cls._mark_lost(reason)
            return False
        return True

    @classmethod
    def stop(cls) -> None:
        with cls._lock:
            lease_handle = cls._lease_handle
            stop_event = cls._stop_event
            heartbeat_thread = cls._heartbeat_thread
            watchdog_thread = cls._watchdog_thread
            if cls._stopping:
                raise RuntimeError('二区 Runtime 进程租约停止尚未完成')
            cls._stopping = True
            if stop_event is not None:
                stop_event.set()
            thread_stop_timeout = min(
                _LEASE_THREAD_STOP_TIMEOUT_MAX_SECONDS,
                max(
                    1.0,
                    cls._heartbeat_interval_seconds
                    + cls._heartbeat_timeout_seconds,
                ),
            )

        current_thread = threading.current_thread()
        deadline = time.monotonic() + thread_stop_timeout
        alive_threads = []
        # The watchdog only waits on the stop event and should retire first;
        # preserve the remaining shared deadline for a heartbeat that may be
        # returning from PostgreSQL I/O.
        for thread in (watchdog_thread, heartbeat_thread):
            if thread is None:
                continue
            if thread is current_thread:
                alive_threads.append(thread)
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                alive_threads.append(thread)
        if alive_threads:
            # Never close or unlock a SQLAlchemy Connection while its heartbeat
            # thread may still be executing on it. Keep the session advisory
            # lease referenced until process death and make host shutdown fail
            # closed so no replacement owner can overlap this process.
            raise RuntimeError('二区 Runtime 进程租约心跳线程未停止')

        with cls._lock:
            if (
                cls._lease_handle is not lease_handle
                or cls._stop_event is not stop_event
            ):
                raise RuntimeError('二区 Runtime 进程租约停止所有权变化')
            was_lost = cls._lost
            cls._lease_handle = None
            cls._connection = None
            cls._app = None
            cls._backend_pid = None
            cls._last_heartbeat_monotonic = None
            cls._stop_event = None
            cls._heartbeat_thread = None
            cls._watchdog_thread = None

        try:
            if lease_handle is None:
                return
            if was_lost:
                lease_handle.discard()
                return
            if not lease_handle.release():
                logger.warning(
                    '二区 Runtime 进程租约连接未持有 advisory lock'
                )
        finally:
            with cls._lock:
                cls._lost = False
                cls._fail_stop_started = False
                cls._stopping = False
