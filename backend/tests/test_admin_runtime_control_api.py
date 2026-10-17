from db_repository import db_manager
from models import Tavern
from services.runtime_supervisor_service import RuntimeSupervisorService


class TestAdminRuntimeControlApi:
    def _create_tavern(self, client, headers, *, slug="manual-runtime"):
        response = client.post(
            "/api/admin/taverns",
            headers=headers,
            json={"display_name": "手动点火旅店", "slug": slug},
        )
        assert response.status_code == 201
        return response.json["data"]

    def test_admin_can_start_stop_and_restart_runtime(
        self,
        app,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("runtime-admin", is_admin=True)
        tavern_data = self._create_tavern(client, admin_headers)
        calls = []

        def set_runtime_state(tavern, status, pid):
            tavern.runtime_status = status
            tavern.runtime_pid = pid
            ok, error = db_manager.commit()
            assert ok, error

        def fake_start(tavern, user):
            calls.append(("start", tavern.id, user.username))
            set_runtime_state(tavern, "ready", 31001)
            return True, None

        def fake_stop(tavern):
            calls.append(("stop", tavern.id, None))
            set_runtime_state(tavern, "stopped", None)
            return True, None

        def fake_restart(tavern, user):
            calls.append(("restart", tavern.id, user.username))
            set_runtime_state(tavern, "ready", 31002)
            return True, None

        monkeypatch.setattr(RuntimeSupervisorService, "ensure_ready", fake_start)
        monkeypatch.setattr(RuntimeSupervisorService, "stop_runtime", fake_stop)
        monkeypatch.setattr(RuntimeSupervisorService, "restart_runtime", fake_restart)

        start_response = client.post(
            f"/api/admin/taverns/{tavern_data['id']}/runtime/start",
            headers=admin_headers,
        )
        assert start_response.status_code == 200
        assert start_response.json["data"]["runtime_status"] == "ready"
        assert start_response.json["data"]["runtime_pid"] == 31001

        stop_response = client.post(
            f"/api/admin/taverns/{tavern_data['id']}/runtime/stop",
            headers=admin_headers,
        )
        assert stop_response.status_code == 200
        assert stop_response.json["data"]["runtime_status"] == "stopped"
        assert stop_response.json["data"]["runtime_pid"] is None

        restart_response = client.post(
            f"/api/admin/taverns/{tavern_data['id']}/runtime/restart",
            headers=admin_headers,
        )
        assert restart_response.status_code == 200
        assert restart_response.json["data"]["runtime_status"] == "ready"
        assert restart_response.json["data"]["runtime_pid"] == 31002
        assert calls == [
            ("start", tavern_data["id"], "runtime-admin"),
            ("stop", tavern_data["id"], None),
            ("restart", tavern_data["id"], "runtime-admin"),
        ]

    def test_runtime_control_requires_admin(
        self,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        _, player_headers = make_user("player")
        tavern = self._create_tavern(
            client,
            admin_headers,
            slug="admin-only-runtime",
        )
        called = False

        def unexpected(*args, **kwargs):
            nonlocal called
            called = True
            return True, None

        monkeypatch.setattr(
            RuntimeSupervisorService,
            "ensure_ready",
            unexpected,
        )
        response = client.post(
            f"/api/admin/taverns/{tavern['id']}/runtime/start",
            headers=player_headers,
        )
        assert response.status_code == 403
        assert called is False

    def test_disabled_tavern_cannot_start_or_restart(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("disabled-admin", is_admin=True)
        tavern = self._create_tavern(
            client,
            admin_headers,
            slug="disabled-runtime",
        )
        disabled_response = client.put(
            f"/api/admin/taverns/{tavern['id']}",
            headers=admin_headers,
            json={"status": "disabled"},
        )
        assert disabled_response.status_code == 200

        for action in ("start", "restart"):
            response = client.post(
                f"/api/admin/taverns/{tavern['id']}/runtime/{action}",
                headers=admin_headers,
            )
            assert response.status_code == 409
            assert response.json["message"] == "请先重新开放酒馆"

    def test_stop_is_idempotent_when_runtime_has_no_process(
        self,
        app,
        client,
        make_user,
    ):
        _, admin_headers = make_user("idempotent-admin", is_admin=True)
        tavern_data = self._create_tavern(
            client,
            admin_headers,
            slug="idempotent-stop",
        )

        for _ in range(2):
            response = client.post(
                f"/api/admin/taverns/{tavern_data['id']}/runtime/stop",
                headers=admin_headers,
            )
            assert response.status_code == 200
            assert response.json["data"]["runtime_status"] == "stopped"
            assert response.json["data"]["runtime_pid"] is None

        with app.app_context():
            tavern = db_manager.query(Tavern).filter_by(
                id=tavern_data["id"]
            ).one()
            assert tavern.runtime_status == "stopped"
