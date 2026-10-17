import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class SchedulerService:
    _app = None
    _thread = None
    _stop = threading.Event()
    _jobs = []
    _jobs_lock = threading.RLock()

    @classmethod
    def init_app(cls, app):
        if cls._app is app and cls._thread and cls._thread.is_alive():
            return
        with app.app_context():
            adapter = app.extensions["game_pack_adapter"]
            definitions = adapter.get_scheduler_tasks()
        now = time.monotonic()
        cls._jobs = [
            {
                **definition,
                "next_run": (
                    now + cls._interval_seconds(definition)
                    if definition["trigger"] == "interval"
                    else None
                ),
                "last_cron_date": None,
                "running": False,
            }
            for definition in definitions
        ]
        cls._app = app
        cls._stop.clear()
        cls._thread = threading.Thread(
            target=cls._loop,
            daemon=True,
            name=f"runtime-scheduler-{app.config['GAME_RUNTIME_ID'][:8]}",
        )
        cls._thread.start()

    @staticmethod
    def _interval_seconds(job):
        return max(
            1,
            int(job.get("seconds", 0))
            + int(job.get("minutes", 0)) * 60
            + int(job.get("hours", 0)) * 3600,
        )

    @classmethod
    def reschedule_interval_job(cls, job_id, **interval_kwargs):
        if not interval_kwargs or set(interval_kwargs) - {"seconds", "minutes", "hours"}:
            return False, "必须提供 seconds、minutes 或 hours 间隔"
        try:
            values = {key: int(value) for key, value in interval_kwargs.items()}
        except (TypeError, ValueError):
            return False, "调度间隔必须是整数"
        if any(value < 0 for value in values.values()) or not any(values.values()):
            return False, "调度间隔必须大于零"
        with cls._jobs_lock:
            job = next((job for job in cls._jobs if job["id"] == job_id), None)
            if job is None or job["trigger"] != "interval":
                return False, "未找到 interval 调度任务"
            for key in ("seconds", "minutes", "hours"):
                job.pop(key, None)
            job.update(values)
            job["next_run"] = time.monotonic() + cls._interval_seconds(job)
        return True, None

    @classmethod
    def _loop(cls):
        while not cls._stop.wait(1):
            monotonic_now = time.monotonic()
            local_now = datetime.now(BEIJING_TZ)
            for job in cls._jobs:
                with cls._jobs_lock:
                    if job["running"]:
                        continue
                    if job["trigger"] == "interval":
                        due = monotonic_now >= job["next_run"]
                    else:
                        due = (
                            local_now.hour == int(job.get("hour", 0))
                            and local_now.minute == int(job.get("minute", 0))
                            and job["last_cron_date"] != local_now.date()
                        )
                    if not due:
                        continue
                    job["running"] = True
                    if job["trigger"] == "interval":
                        job["next_run"] = (
                            monotonic_now + cls._interval_seconds(job)
                        )
                    else:
                        job["last_cron_date"] = local_now.date()
                threading.Thread(
                    target=cls._run_job,
                    args=(job,),
                    daemon=True,
                    name=job["id"],
                ).start()

    @classmethod
    def _run_job(cls, job):
        with cls._app.app_context():
            try:
                job["func"]()
                from db_repository import db_manager

                ok, error = db_manager.commit()
                if not ok:
                    raise RuntimeError(error)
            except Exception:
                from db_repository import db_manager

                db_manager.rollback()
                logger.exception("Runtime scheduler job failed: %s", job["id"])
            finally:
                from db_repository import db_manager

                db_manager.remove()
                with cls._jobs_lock:
                    job["running"] = False

    @classmethod
    def stop(cls):
        cls._stop.set()
        if cls._thread and cls._thread.is_alive():
            cls._thread.join(timeout=3)
