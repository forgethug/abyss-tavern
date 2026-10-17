from types import SimpleNamespace
from unittest.mock import Mock

from services.runtime_supervisor_service import RuntimeSupervisorService as Supervisor


def test_unidentified_live_runtime_never_reports_stopped(app, monkeypatch):
    tavern = SimpleNamespace(runtime_namespace='isolated-stop', runtime_pid=None)
    monkeypatch.setattr(Supervisor, '_processes', {})
    monkeypatch.setattr(Supervisor, '_owned_runtime_processes', lambda _: [])
    monkeypatch.setattr(Supervisor, '_health', lambda _: True)
    mark = Mock()
    monkeypatch.setattr(Supervisor, '_mark_stopped', mark)
    with app.app_context():
        assert Supervisor.stop_runtime(tavern)[0] is False
    mark.assert_not_called()


def test_unrelated_reused_pid_is_never_terminated(app, monkeypatch):
    tavern = SimpleNamespace(runtime_namespace='isolated-stop', runtime_pid=12345)
    monkeypatch.setattr(Supervisor, '_processes', {})
    monkeypatch.setattr(Supervisor, '_owned_runtime_processes', lambda _: [])
    monkeypatch.setattr(Supervisor, '_pid_exists', lambda _: True)
    terminate = Mock()
    monkeypatch.setattr(Supervisor, '_terminate_targets', terminate)
    with app.app_context():
        assert Supervisor.stop_runtime(tavern)[0] is False
    terminate.assert_not_called()
