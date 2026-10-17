import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from runtime_core.task_registry import runtime_task_registry


logger = logging.getLogger(__name__)


class AsyncTaskService:
    """Shared in-process queue; each pack owns its registered task semantics."""

    SUPPORTED_TASK_TYPES = frozenset()
    _app = None
    _queue = None
    _worker = None
    _executor = None
    _active_keys = set()
    _running_keys = set()
    _pending_tasks = 0
    _inflight_tasks = 0
    _active_keys_lock = threading.Lock()

    @classmethod
    def _refresh_supported_types(cls):
        cls.SUPPORTED_TASK_TYPES = runtime_task_registry.supported_types()

    @classmethod
    def init_app(cls, app):
        cls._refresh_supported_types()
        if cls._app is app and cls._worker and cls._worker.is_alive():
            return
        cls._app = app
        cls._queue = queue.Queue()
        runtime_id = app.config["GAME_RUNTIME_ID"]
        cls._executor = ThreadPoolExecutor(
            max_workers=max(
                1,
                int(app.config.get("ASYNC_TASK_MAX_WORKERS", 4)),
            ),
            thread_name_prefix=f"runtime-ai-{runtime_id[:8]}",
        )
        cls._worker = threading.Thread(
            target=cls._worker_loop,
            daemon=True,
            name=f"runtime-queue-{runtime_id[:8]}",
        )
        cls._worker.start()

    @classmethod
    def _build_task_dedupe_key(cls, task_type, payload):
        return runtime_task_registry.build_dedupe_key(
            task_type,
            payload if isinstance(payload, dict) else {},
        )

    @staticmethod
    def _key(task_type, payload, *fields):
        values = [payload.get(field) for field in fields]
        if not values or not values[0]:
            return None
        return ":".join(
            [task_type, *(str(value or "") for value in values)]
        )

    @classmethod
    def submit_task(cls, task_type, payload):
        cls._refresh_supported_types()
        if not runtime_task_registry.is_registered(task_type):
            logger.error("Unsupported runtime async task type: %s", task_type)
            return False
        if cls._queue is None:
            if cls._app is None:
                raise RuntimeError("AsyncTaskService is not initialized")
            cls.init_app(cls._app)
        dedupe_key = cls._build_task_dedupe_key(task_type, payload)
        if dedupe_key:
            with cls._active_keys_lock:
                if dedupe_key in cls._active_keys:
                    return False
                cls._active_keys.add(dedupe_key)
        with cls._active_keys_lock:
            cls._pending_tasks += 1
        cls._queue.put(
            {
                "type": task_type,
                "payload": payload or {},
                "dedupe_key": dedupe_key,
            }
        )
        return True

    @classmethod
    def has_durable_queue(cls):
        return False

    @classmethod
    def can_run_database_startup_recovery(cls):
        # Each portable tavern owns its embedded consumer and its database.
        return bool(cls._app is not None and cls._worker and cls._worker.is_alive())

    @classmethod
    def get_target_worker_boot_id(cls):
        if not cls.can_run_database_startup_recovery():
            return None
        from utils.backend_boot import get_backend_boot_id

        return get_backend_boot_id()

    @classmethod
    def has_exclusive_startup_ownership(cls):
        # The embedded queue has no cross-process consumer ownership receipt.
        # Let pack recovery use its persisted stale-work checks instead.
        return False

    @classmethod
    def has_restart_recovered_queued_task(cls, task_type, payload):
        # Deliveries are volatile; only pack database work can be recovered.
        return False

    @classmethod
    def get_task_delivery_state(cls, task_type, payload):
        key = cls._build_task_dedupe_key(task_type, payload)
        with cls._active_keys_lock:
            if not key or key not in cls._active_keys:
                return "missing"
            return "running_or_locked" if key in cls._running_keys else "queued"

    @classmethod
    def get_runtime_status(cls):
        with cls._active_keys_lock:
            pending = cls._pending_tasks
            inflight = cls._inflight_tasks
        return {
            "alive": cls.can_run_database_startup_recovery(),
            "process_role": "embedded",
            "execution_backend": "thread",
            "max_workers": int(cls._app.config.get("ASYNC_TASK_MAX_WORKERS", 4)) if cls._app else 0,
            "inflight_tasks": inflight,
            "queue_depth": pending,
            "reserved_tasks": 0,
        }

    @classmethod
    def is_task_enqueued_or_running(cls, task_type, payload):
        key = cls._build_task_dedupe_key(task_type, payload)
        if not key:
            return False
        with cls._active_keys_lock:
            return key in cls._active_keys

    @classmethod
    def force_release_task_lock(cls, task_type, payload):
        key = cls._build_task_dedupe_key(task_type, payload)
        cls._release_task_dedupe_key(key)
        return bool(key)

    @classmethod
    def _release_task_dedupe_key(cls, key):
        if not key:
            return
        with cls._active_keys_lock:
            cls._active_keys.discard(key)

    @classmethod
    def _worker_loop(cls):
        while True:
            task = cls._queue.get()
            cls._executor.submit(cls._execute_task, task)

    @classmethod
    def _execute_task(cls, task):
        task_type = task["type"]
        payload = task["payload"]
        with cls._active_keys_lock:
            cls._pending_tasks -= 1
            cls._inflight_tasks += 1
            if task.get("dedupe_key"):
                cls._running_keys.add(task["dedupe_key"])
        with cls._app.app_context():
            try:
                cls._dispatch(task_type, payload)
            except Exception:
                logger.exception(
                    "Runtime async task failed: %s",
                    task_type,
                )
            finally:
                from db_repository import db_manager

                db_manager.remove()
                with cls._active_keys_lock:
                    cls._inflight_tasks -= 1
                    cls._running_keys.discard(task.get("dedupe_key"))
                cls._release_task_dedupe_key(task.get("dedupe_key"))

    @staticmethod
    def _dispatch(task_type, payload):
        runtime_task_registry.dispatch(task_type, payload)
