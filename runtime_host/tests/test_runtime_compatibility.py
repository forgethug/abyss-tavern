import queue
import threading

import pytest

from db_repository import db_manager
from services.async_task_service import AsyncTaskService


def test_volatile_delivery_reports_pending_running_and_completion(runtime_app, monkeypatch):
    service = AsyncTaskService
    monkeypatch.setattr(service, "_app", runtime_app)
    monkeypatch.setattr(service, "_queue", queue.Queue())
    monkeypatch.setattr(service, "_active_keys", set())
    monkeypatch.setattr(service, "_running_keys", set())
    monkeypatch.setattr(service, "_pending_tasks", 0)
    monkeypatch.setattr(service, "_inflight_tasks", 0)
    entered = threading.Event()
    release = threading.Event()

    def dispatch(_task_type, _payload):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(service, "_dispatch", dispatch)
    payload = {"party_id": 1, "expedition_log_id": 7}
    assert service.submit_task("ai_s1_expedition", payload)
    assert not service.submit_task("ai_s1_expedition", payload)
    assert service.get_task_delivery_state("ai_s1_expedition", payload) == "queued"
    assert service.get_runtime_status()["queue_depth"] == 1
    thread = threading.Thread(target=service._execute_task, args=(service._queue.get(),))
    thread.start()
    try:
        assert entered.wait(5)
        assert service.get_task_delivery_state("ai_s1_expedition", payload) == "running_or_locked"
        assert service.get_runtime_status()["inflight_tasks"] == 1
        assert service.get_runtime_status()["queue_depth"] == 0
        assert not service.has_restart_recovered_queued_task("ai_s1_expedition", payload)
        assert not service.has_exclusive_startup_ownership()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert service.get_task_delivery_state("ai_s1_expedition", payload) == "missing"
    assert service.get_runtime_status()["inflight_tasks"] == 0


def test_building_deliveries_are_bound_to_usage_session(runtime_app):
    base = {"adventurer_id": 4, "usage_session_id": "first"}
    second = {**base, "usage_session_id": "second"}
    for task_type in (
        "ai_healing_house", "ai_blacksmith", "ai_tailor_workshop",
        "ai_alchemy_workshop", "ai_item_assembly_workshop", "ai_ember_kitchen",
        "ai_chapel", "ai_skill_training", "ai_pet_evolution",
    ):
        assert AsyncTaskService._build_task_dedupe_key(task_type, base) != (
            AsyncTaskService._build_task_dedupe_key(task_type, second)
        )


def test_transaction_depth_resets_on_error(runtime_app):
    with runtime_app.app_context():
        assert db_manager.transaction_depth() == 0
        with pytest.raises(Exception, match="abort"):
            with db_manager.transaction():
                assert db_manager.transaction_depth() == 1
                raise ValueError("abort")
        assert db_manager.transaction_depth() == 0
