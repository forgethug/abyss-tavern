"""Bounded startup diagnostics; never include configuration or player data."""

from contextlib import contextmanager
from time import perf_counter


@contextmanager
def startup_phase(name):
    started = perf_counter()
    print(f"[runtime startup] {name} started", flush=True)
    try:
        yield
    except BaseException:
        print(f"[runtime startup] {name} failed", flush=True)
        raise
    else:
        print(
            f"[runtime startup] {name} completed {perf_counter() - started:.3f}s",
            flush=True,
        )
