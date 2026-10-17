import json

from db_repository import db_manager
from models import Tavern, User
from services.runtime_supervisor_service import RuntimeSupervisorService


class TestRuntimeProxyApi:
    def test_only_platform_admin_can_proxy_shared_narrative_api(
        self,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("narrative-admin", is_admin=True)
        _, player_headers = make_user("narrative-player")
        tavern = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "叙事隔离测试",
                "slug": "narrative-admin",
                "game_pack": "s1_fantasy",
            },
        ).json["data"]
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "ensure_ready",
            staticmethod(lambda tavern, user: (True, None)),
        )
        captured = {}

        def fake_proxy(tavern, **kwargs):
            captured.update(kwargs)
            return (
                json.dumps(
                    {
                        "code": 200,
                        "message": "success",
                        "data": {"pack_id": tavern.game_pack},
                    }
                ).encode(),
                200,
                {"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            RuntimeSupervisorService,
            "proxy_narrative_request",
            staticmethod(fake_proxy),
        )
        denied = client.get(
            f"/api/admin/taverns/{tavern['id']}/narrative/descriptor",
            headers=player_headers,
        )
        assert denied.status_code == 403
        assert captured == {}

        response = client.post(
            (
                f"/api/admin/taverns/{tavern['id']}/narrative/"
                "preview?mode=admin"
            ),
            headers={
                **admin_headers,
                "Content-Type": "application/json",
            },
            json={"run_key": "preview-1"},
        )
        assert response.status_code == 200
        assert response.json["data"]["pack_id"] == "s1_fantasy"
        assert captured["narrative_path"] == "preview"
        assert captured["query_string"] == b"mode=admin"
        assert json.loads(captured["body"]) == {
            "run_key": "preview-1"
        }

    def test_failed_runtime_start_terminates_child_and_clears_pid(
        self,
        app,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("cleanup-admin", is_admin=True)
        tavern_data = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "熄火测试", "slug": "startup-cleanup"},
        ).json["data"]

        class FakeProcess:
            pid = 424242

            def __init__(self):
                self.terminated = False

            def poll(self):
                return 1 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 1

            def kill(self):
                self.terminated = True

            def children(self, recursive=False):
                return []

        fake_process = FakeProcess()
        monkeypatch.setattr('services.runtime_supervisor_service.psutil.Process', lambda pid: fake_process)
        monkeypatch.setattr('services.runtime_supervisor_service.psutil.wait_procs', lambda targets, timeout: (targets, []))
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "_launch",
            staticmethod(lambda tavern: fake_process),
        )
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "_health",
            staticmethod(lambda tavern: False),
        )

        with app.app_context():
            app.config["RUNTIME_START_TIMEOUT_SECONDS"] = 0
            tavern = db_manager.query(Tavern).filter_by(
                id=tavern_data["id"]
            ).one()
            user = db_manager.query(User).filter_by(
                username="cleanup-admin"
            ).one()
            ready, error = RuntimeSupervisorService.ensure_ready(tavern, user)

            assert ready is False
            assert error == "Runtime health check timed out"
            assert fake_process.terminated is True
            assert tavern.runtime_pid is None
            assert tavern.runtime_status == "failed"
            assert (
                tavern.runtime_namespace
                not in RuntimeSupervisorService._processes
            )

    def test_member_can_proxy_and_status_is_preserved(
        self,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        player_id, player_headers = make_user("player")
        tavern = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "回声井", "slug": "echo-well"},
        ).json["data"]
        client.post(
            f"/api/admin/taverns/{tavern['id']}/members",
            headers=admin_headers,
            json={"user_id": player_id},
        )

        monkeypatch.setattr(
            RuntimeSupervisorService,
            "ensure_ready",
            staticmethod(lambda tavern, user: (True, None)),
        )

        captured = {}

        def fake_proxy(tavern, **kwargs):
            captured.update(kwargs)
            return (
                json.dumps({"code": 202, "message": "queued"}).encode(),
                202,
                {"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            RuntimeSupervisorService,
            "proxy_request",
            staticmethod(fake_proxy),
        )
        response = client.post(
            "/api/taverns/echo-well/runtime/expeditions?dry_run=1",
            headers={**player_headers, "Content-Type": "application/json"},
            json={"party_id": 7},
        )

        assert response.status_code == 202
        assert response.json["message"] == "queued"
        assert captured["runtime_path"] == "expeditions"
        assert captured["query_string"] == b"dry_run=1"
        assert json.loads(captured["body"]) == {"party_id": 7}

    def test_non_member_cannot_reach_runtime(
        self,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        _, stranger_headers = make_user("stranger")
        client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "封闭回廊", "slug": "sealed-hall"},
        )

        called = False

        def unexpected(*args, **kwargs):
            nonlocal called
            called = True
            return True, None

        monkeypatch.setattr(RuntimeSupervisorService, "ensure_ready", unexpected)
        response = client.get(
            "/api/taverns/sealed-hall/runtime/info",
            headers=stranger_headers,
        )

        assert response.status_code == 403
        assert called is False

    def test_runtime_start_failure_is_service_unavailable(
        self,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        player_id, player_headers = make_user("player")
        tavern = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "尚未点火", "slug": "not-lit"},
        ).json["data"]
        client.post(
            f"/api/admin/taverns/{tavern['id']}/members",
            headers=admin_headers,
            json={"user_id": player_id},
        )
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "ensure_ready",
            staticmethod(lambda tavern, user: (False, "启动超时")),
        )

        response = client.get(
            "/api/taverns/not-lit/runtime/info",
            headers=player_headers,
        )
        assert response.status_code == 503
        assert response.json["message"] == "启动超时"
