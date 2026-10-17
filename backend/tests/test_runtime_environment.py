from pathlib import Path

from db_repository import db_manager
from models import Tavern, User
from services.runtime_supervisor_service import RuntimeSupervisorService


class TestRuntimeEnvironment:
    def _create_tavern(self, client, headers):
        response = client.post(
            "/api/admin/taverns",
            headers=headers,
            json={"display_name": "环境隔离酒馆", "slug": "env-isolation"},
        )
        assert response.status_code == 201
        return response.json["data"]

    def test_child_environment_is_allowlisted_and_formats_database_url(
        self,
        app,
        client,
        make_user,
        monkeypatch,
        tmp_path: Path,
    ):
        _, admin_headers = make_user("env-admin", is_admin=True)
        tavern_data = self._create_tavern(client, admin_headers)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "allowed-ai-secret")
        monkeypatch.setenv("STOCK_PROVIDER_SECRET", "must-not-leak")

        with app.app_context():
            app.config["RUNTIME_DATA_DIR"] = str(tmp_path)
            app.config["RUNTIME_DATABASE_URL_TEMPLATE"] = (
                "postgresql+psycopg://runtime:test@localhost/"
                "s1_{runtime_key}_{tavern_id}"
            )
            app.config["RUNTIME_MODEL_PROFILES_JSON"] = (
                '[{"id":"primary","api_base":"https://models.test/v1",'
                '"api_key":"central-secret","model":"story"}]'
            )
            app.config["RUNTIME_MODEL_ASSIGNMENTS_JSON"] = (
                '{"narrative_generation":"primary"}'
            )
            app.config["RUNTIME_MODEL_REASONING_JSON"] = (
                '{"narrative_generation":"high"}'
            )
            tavern = db_manager.query(Tavern).filter_by(
                id=tavern_data["id"]
            ).one()
            environment = RuntimeSupervisorService._child_environment(
                tavern,
                tmp_path,
            )

            assert environment["DEEPSEEK_API_KEY"] == "allowed-ai-secret"
            assert "STOCK_PROVIDER_SECRET" not in environment
            assert environment["GAME_RUNTIME_ID"] == tavern.runtime_namespace
            assert environment["GAME_RUNTIME_DATA_DIR"] == str(tmp_path)
            assert environment["GAME_RUNTIME_PACK_ID"] == "s1_fantasy"
            assert environment["GAME_RUNTIME_API_VERSION"] == "0.1"
            assert environment["GAME_RUNTIME_MODEL_CAPABILITIES_JSON"] == (
                '["narrative_generation", "structured_generation", '
                '"semantic_review"]'
            )
            assert (
                environment["GAME_RUNTIME_MODEL_PROFILES_JSON"]
                == app.config["RUNTIME_MODEL_PROFILES_JSON"]
            )
            assert (
                environment["GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON"]
                == app.config["RUNTIME_MODEL_ASSIGNMENTS_JSON"]
            )
            assert (
                environment["GAME_RUNTIME_MODEL_REASONING_JSON"]
                == app.config["RUNTIME_MODEL_REASONING_JSON"]
            )
            assert environment["GAME_RUNTIME_DATABASE_URL"].endswith(
                f"s1_{tavern.runtime_namespace.replace('-', '')}_{tavern.id}"
            )

    def test_launch_failure_is_persisted_without_pid(
        self,
        app,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("launch-failure-admin", is_admin=True)
        tavern_data = self._create_tavern(client, admin_headers)
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "_health",
            staticmethod(lambda tavern: False),
        )
        monkeypatch.setattr(
            RuntimeSupervisorService,
            "_launch",
            staticmethod(
                lambda tavern: (_ for _ in ()).throw(
                    OSError("executable unavailable")
                )
            ),
        )

        with app.app_context():
            tavern = db_manager.query(Tavern).filter_by(
                id=tavern_data["id"]
            ).one()
            user = db_manager.query(User).filter_by(
                username="launch-failure-admin"
            ).one()
            ready, error = RuntimeSupervisorService.ensure_ready(tavern, user)

            assert ready is False
            assert error == (
                "Runtime 进程启动失败: executable unavailable"
            )
            assert tavern.runtime_status == "failed"
            assert tavern.runtime_pid is None

    def test_invalid_database_template_is_rejected_without_secret_echo(
        self,
        app,
        client,
        make_user,
        tmp_path: Path,
    ):
        _, admin_headers = make_user("template-admin", is_admin=True)
        tavern_data = self._create_tavern(client, admin_headers)

        with app.app_context():
            app.config["RUNTIME_DATABASE_URL_TEMPLATE"] = (
                "postgresql://secret@localhost/{unknown_placeholder}"
            )
            tavern = db_manager.query(Tavern).filter_by(
                id=tavern_data["id"]
            ).one()

            try:
                RuntimeSupervisorService._child_environment(tavern, tmp_path)
            except ValueError as exc:
                assert str(exc) == (
                    "RUNTIME_DATABASE_URL_TEMPLATE 包含无效占位符"
                )
                assert "secret" not in str(exc)
            else:
                raise AssertionError("invalid template must be rejected")
