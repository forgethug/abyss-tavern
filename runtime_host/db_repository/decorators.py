import logging
import time
from functools import wraps

from db_repository.exceptions import DatabaseError
from extensions import db


logger = logging.getLogger(__name__)


def monitor_performance(operation_name=None, context_getter=None, slow_threshold=5.0):
    """Observe pack operations without changing their transaction or result."""
    def decorator(function):
        name = operation_name or f"{function.__module__}.{function.__qualname__}"

        @wraps(function)
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - started
                if elapsed > slow_threshold:
                    logger.warning("Slow DB operation: %s took %.3fs", name, elapsed)

        return wrapped

    return decorator


def with_transaction(auto_commit: bool = True):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
                if auto_commit:
                    db.session.commit()
                return result
            except Exception:
                db.session.rollback()
                raise

        return wrapped

    return decorator


def db_retry(max_retries: int = 3, delay: float = 0.05, **_options):
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return function(*args, **kwargs)
                except DatabaseError as exc:
                    last_error = exc
                    db.session.rollback()
                    if attempt + 1 < max_retries:
                        time.sleep(delay * (attempt + 1))
            raise last_error

        return wrapped

    return decorator


def get_operation_stats() -> dict:
    return {}


def reset_operation_stats() -> None:
    return None
