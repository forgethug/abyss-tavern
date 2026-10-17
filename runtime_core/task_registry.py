import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional


TaskHandler = Callable[[dict], None]
DedupeKeyBuilder = Callable[[dict], Optional[str]]


@dataclass(frozen=True)
class RegisteredTask:
    task_type: str
    handler: TaskHandler
    dedupe_key_builder: Optional[DedupeKeyBuilder]


class RuntimeTaskRegistry:
    """Process-local task contract populated by exactly one game pack."""

    def __init__(self):
        self._tasks: Dict[str, RegisteredTask] = {}
        self._lock = threading.RLock()

    def register(
        self,
        task_type: str,
        handler: TaskHandler,
        *,
        dedupe_key_builder: Optional[DedupeKeyBuilder] = None,
    ) -> None:
        normalized_type = str(task_type or "").strip()
        if not normalized_type:
            raise ValueError("task_type must be non-empty")
        if not callable(handler):
            raise TypeError("task handler must be callable")
        if dedupe_key_builder is not None and not callable(dedupe_key_builder):
            raise TypeError("dedupe_key_builder must be callable")
        with self._lock:
            if normalized_type in self._tasks:
                raise ValueError(
                    f"Runtime task type already registered: {normalized_type}"
                )
            self._tasks[normalized_type] = RegisteredTask(
                task_type=normalized_type,
                handler=handler,
                dedupe_key_builder=dedupe_key_builder,
            )

    def supported_types(self) -> frozenset:
        with self._lock:
            return frozenset(self._tasks)

    def is_registered(self, task_type: str) -> bool:
        with self._lock:
            return task_type in self._tasks

    def build_dedupe_key(self, task_type: str, payload: dict) -> Optional[str]:
        with self._lock:
            registered = self._tasks.get(task_type)
        if registered is None or registered.dedupe_key_builder is None:
            return None
        return registered.dedupe_key_builder(
            payload if isinstance(payload, dict) else {}
        )

    def dispatch(self, task_type: str, payload: dict) -> None:
        with self._lock:
            registered = self._tasks.get(task_type)
        if registered is None:
            raise ValueError(f"Unsupported runtime task type: {task_type}")
        registered.handler(payload if isinstance(payload, dict) else {})

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()


def dedupe_by_fields(task_type: str, *fields: str) -> DedupeKeyBuilder:
    if not fields:
        raise ValueError("at least one dedupe field is required")

    def build(payload: dict) -> Optional[str]:
        values = [payload.get(field) for field in fields]
        if not values[0]:
            return None
        return ":".join(
            [task_type, *(str(value or "") for value in values)]
        )

    return build


def global_dedupe_key(task_type: str) -> DedupeKeyBuilder:
    return lambda _payload: f"{task_type}:global"


runtime_task_registry = RuntimeTaskRegistry()
