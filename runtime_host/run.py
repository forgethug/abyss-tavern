import os

from startup import startup_phase

with startup_phase("imports"):
    from app import create_runtime_app


with startup_phase("application"):
    app = create_runtime_app()
from services.async_task_service import AsyncTaskService
from services.scheduler_service import SchedulerService

with startup_phase("workers"):
    AsyncTaskService.init_app(app)
    with app.app_context():
        app.extensions["game_pack_adapter"].after_async_task_init(app)
if os.getenv(
    "GAME_RUNTIME_SCHEDULER_ENABLED",
    "true",
).lower() == "true":
    with startup_phase("scheduler"):
        SchedulerService.init_app(app)


if __name__ == "__main__":
    from waitress import serve
    print("[runtime startup] http starting", flush=True)
    serve(
        app,
        host=os.getenv(
            "GAME_RUNTIME_HOST",
            "127.0.0.1",
        ),
        port=int(
            os.getenv(
                "GAME_RUNTIME_PORT",
                "5101",
            )
        ),
        threads=8,
    )
