from runtime_core.time import utc_now
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
from io import BytesIO

import pytest

from db_repository import db_manager
from extensions import db
from models import Tavern, TavernMembership, User
from services.runtime_supervisor_service import RuntimeSupervisorService as Supervisor
from services.tavern_service import TavernService


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.exited = False

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.exited = True

    def wait(self, timeout):
        return 0


@pytest.fixture
def running(app, make_user, monkeypatch):
    user_id, headers = make_user("cache-player")
    clock = [10.0]
    monkeypatch.setattr("services.runtime_supervisor_service.time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda _: None,
    ))
    monkeypatch.setattr(Supervisor, "_processes", {})
    health = Mock(return_value=True)
    sync = Mock(return_value=({}, None, 200))
    monkeypatch.setattr(Supervisor, "_health", health)
    monkeypatch.setattr(Supervisor, "_request_json", sync)

    def create(slug):
        with app.app_context():
            user = db_manager.query(User).filter_by(id=user_id).one()
            data, error = TavernService.create_tavern(user, slug, slug)
            assert error is None
            tavern = db_manager.query(Tavern).filter_by(id=data["id"]).one()
            process = Process(90000 + tavern.id)
            tavern.runtime_pid = process.pid
            tavern.runtime_started_at = utc_now()
            tavern.runtime_status = "ready"
            assert db_manager.commit()[0]
            Supervisor._processes[tavern.runtime_namespace] = process
            return tavern.id, tavern.runtime_namespace, process

    tavern_id, namespace, process = create("cache-tavern")

    def ready(target_id=None, actor_id=None):
        with app.app_context():
            tavern = db_manager.query(Tavern).filter_by(id=target_id or tavern_id).one()
            user = db_manager.query(User).filter_by(id=actor_id or user_id).one()
            return Supervisor.ensure_ready(tavern, user)

    return SimpleNamespace(app=app, user_id=user_id, headers=headers, tavern_id=tavern_id,
        namespace=namespace, process=process, clock=clock, health=health, sync=sync,
        ready=ready, create=create)


def test_repeated_reads_skip_health_identity_and_control_plane_commits(running, monkeypatch):
    commit = Mock(wraps=db_manager.commit)
    monkeypatch.setattr(db_manager, "commit", commit)
    for _ in range(5):
        assert running.ready() == (True, None)
    assert running.health.call_count == running.sync.call_count == 1
    assert commit.call_count == 0
    running.clock[0] += 6
    assert running.ready() == (True, None)
    assert running.health.call_count == 2
    assert running.sync.call_count == 1


@pytest.mark.parametrize('app', ['file'], indirect=True)
def test_concurrent_requests_share_one_preparation(running):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def request():
        with running.app.app_context():
            tavern = db_manager.query(Tavern).filter_by(id=running.tavern_id).one()
            user = db_manager.query(User).filter_by(id=running.user_id).one()
            barrier.wait(timeout=5)
            return Supervisor.ensure_ready(tavern, user)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
    errors = [repr(future.exception()) for future in futures if future.exception()]
    assert not errors, errors
    results = [future.result() for future in futures]
    assert results == [(True, None), (True, None)]
    assert running.health.call_count == running.sync.call_count == 1


@pytest.mark.parametrize("field,value", [
    ("display_name", "Changed"), ("is_admin", True), ("is_admin", False),
    ("last_login_at", datetime(2026, 10, 17)),
])
def test_changed_identity_is_synchronized_without_waiting_for_health_ttl(running, field, value):
    if field == "is_admin" and value is False:
        with running.app.app_context():
            db_manager.query(User).filter_by(id=running.user_id).one().is_admin = True
            assert db_manager.commit()[0]
    assert running.ready()[0]
    with running.app.app_context():
        user = db_manager.query(User).filter_by(id=running.user_id).one()
        setattr(user, field, value)
        assert db_manager.commit()[0]
    assert running.ready()[0]
    assert running.health.call_count == 1
    assert running.sync.call_count == 2
    payload = running.sync.call_args.kwargs["payload"]
    key = "nickname" if field == "display_name" else field
    assert payload[key] == (value.isoformat() if isinstance(value, datetime) else value)


def test_same_user_is_synchronized_independently_into_two_taverns(running):
    second_id, _, _ = running.create("second-cache-tavern")
    for tavern_id in (running.tavern_id, second_id, running.tavern_id, second_id):
        assert running.ready(tavern_id)[0]
    assert running.health.call_count == running.sync.call_count == 2
    assert len({call.args[0] for call in running.sync.call_args_list}) == 2


def test_failed_identity_sync_is_not_cached(running):
    running.sync.side_effect = [(None, "sync failed", 503), ({}, None, 200)]
    assert running.ready() == (False, "sync failed")
    assert running.ready()[0]
    assert running.health.call_count == running.sync.call_count == 2


def test_health_failure_does_not_launch_duplicate_live_process(running, monkeypatch):
    assert running.ready()[0]
    running.clock[0] += 6
    running.health.return_value = False
    launch = Mock(side_effect=AssertionError("must not launch a second live process"))
    monkeypatch.setattr(Supervisor, "_launch", launch)
    assert not running.ready()[0]
    running.health.return_value = True
    assert running.ready()[0]
    assert running.sync.call_count == 2
    launch.assert_not_called()


def test_exited_process_is_replaced_and_identity_is_resynchronized(running, monkeypatch):
    assert running.ready()[0]
    running.process.exited = True
    replacement = Process(91000)
    monkeypatch.setattr(Supervisor, "_launch", Mock(return_value=replacement))
    running.health.side_effect = [False, True]
    assert running.ready()[0]
    assert running.sync.call_count == 2
    assert Supervisor._processes[running.namespace] is replacement
    with running.app.app_context():
        assert db_manager.query(Tavern).filter_by(id=running.tavern_id).one().runtime_pid == 91000


def test_stop_restart_cannot_reuse_cached_identity_even_with_reused_pid(running, monkeypatch):
    assert running.ready()[0]
    replacement = Process(running.process.pid)
    monkeypatch.setattr(Supervisor, "_launch", Mock(return_value=replacement))
    monkeypatch.setattr(Supervisor, "_terminate_process", lambda process: (process.terminate() or True))
    running.health.side_effect = [False, False, True]
    with running.app.app_context():
        tavern = db_manager.query(Tavern).filter_by(id=running.tavern_id).one()
        user = db_manager.query(User).filter_by(id=running.user_id).one()
        assert Supervisor.restart_runtime(tavern, user)[0]
    assert running.process.exited
    assert running.sync.call_count == 2
    assert running.ready()[0]
    assert running.sync.call_count == 2


def test_runtime_without_owned_process_handle_is_always_verified(running):
    Supervisor._processes.clear()
    assert running.ready()[0] and running.ready()[0]
    assert running.health.call_count == running.sync.call_count == 2


def test_identity_cache_is_bounded(running, make_user):
    running.app.config["RUNTIME_IDENTITY_CACHE_MAX_USERS"] = 1
    other_id, _ = make_user("other-cache-player")
    for actor in (running.user_id, other_id, running.user_id):
        assert running.ready(actor_id=actor)[0]
    assert running.health.call_count == 1
    assert running.sync.call_count == 3


@pytest.mark.parametrize("kind", ["transport", "business"])
def test_transport_failure_invalidates_without_replaying_write(running, monkeypatch, kind):
    assert running.ready()[0]
    error = TimeoutError("response lost") if kind == "transport" else HTTPError(
        "http://localhost/test", 409, "conflict", {}, BytesIO(b'{"conflict":true}')
    )
    request = Mock(side_effect=error)
    monkeypatch.setattr("services.runtime_supervisor_service.urlopen", request)
    with running.app.app_context():
        tavern = db_manager.query(Tavern).filter_by(id=running.tavern_id).one()
        kwargs = dict(runtime_path="claim", query_string=b"", method="POST", body=b"{}", headers={})
        if kind == "transport":
            with pytest.raises(TimeoutError):
                Supervisor.proxy_request(tavern, **kwargs)
        else:
            assert Supervisor.proxy_request(tavern, **kwargs)[1] == 409
    assert request.call_count == 1
    assert running.ready()[0]
    assert running.health.call_count == running.sync.call_count == (2 if kind == "transport" else 1)


@pytest.mark.parametrize("revocation", ["membership", "account"])
def test_warm_runtime_cache_does_not_bypass_current_access_checks(running, client, monkeypatch, revocation):
    proxy = Mock(return_value=(b'{"ok":true}', 200, {"Content-Type": "application/json"}))
    monkeypatch.setattr(Supervisor, "proxy_request", proxy)
    path = "/api/taverns/cache-tavern/runtime/info"
    assert client.get(path, headers=running.headers).status_code == 200
    with running.app.app_context():
        if revocation == "membership":
            db_manager.query(TavernMembership).filter_by(tavern_id=running.tavern_id,
                user_id=running.user_id).one().status = "disabled"
        else:
            db_manager.query(User).filter_by(id=running.user_id).one().is_active = False
        assert db_manager.commit()[0]
    # The shared test app fixture retains an outer SQLAlchemy identity map;
    # real HTTP requests each start with their own request-scoped session.
    db.session.expire_all()
    assert client.get(path, headers=running.headers).status_code == (403 if revocation == "membership" else 401)
    assert proxy.call_count == 1
    assert running.health.call_count == running.sync.call_count == 1
