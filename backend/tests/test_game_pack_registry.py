from db_repository import db_manager
from models import Tavern
from services.game_pack_registry import (
    GamePackConfigurationError,
    GamePackRegistry,
)
from services.runtime_supervisor_service import RuntimeSupervisorService


class TestGamePackRegistry:
    def test_s1_manifest_is_an_executable_compatible_definition(self):
        definition = GamePackRegistry.get("s1_fantasy")

        assert definition.platform_api_version == "0.1"
        assert definition.runtime_api_version == "0.1"
        assert definition.runtime_entrypoint.name == "run.py"
        assert definition.runtime_entrypoint.is_file()
        assert definition.runtime_adapter.name == "s1_adapter.py"
        assert [path.name for path in definition.runtime_import_paths] == [
            "backend"
        ]
        assert definition.runtime_api_prefix == "/api/adventurer-s1"
        assert definition.frontend_plugin == "adventurer_s1"
        assert definition.model_capabilities == (
            "narrative_generation",
            "structured_generation",
            "semantic_review",
        )


    def test_unknown_pack_is_rejected(self):
        try:
            GamePackRegistry.get("missing_pack")
        except GamePackConfigurationError as exc:
            assert str(exc) == "Unknown game pack: missing_pack"
        else:
            raise AssertionError("unknown game pack must be rejected")

    def test_uninstalled_pack_release_is_rejected(self):
        try:
            GamePackRegistry.require_release("s1_fantasy", "old-release")
        except GamePackConfigurationError as exc:
            assert str(exc) == (
                "Game pack release is not installed: "
                "s1_fantasy@old-release"
            )
        else:
            raise AssertionError("uninstalled release must be rejected")

    def test_admin_can_list_and_select_a_registered_pack(
        self,
        app,
        client,
        make_user,
    ):
        _, admin_headers = make_user("pack-admin", is_admin=True)

        listed = client.get("/api/admin/game-packs", headers=admin_headers)
        assert listed.status_code == 200
        listed_by_id = {
            pack["pack_id"]: pack
            for pack in listed.json["data"]
        }
        assert listed_by_id == {
            "s1_fantasy": {
                "pack_id": "s1_fantasy",
                "display_name": "深渊酒馆",
                "platform_api_version": "0.1",
                "runtime_api_version": "0.1",
                "pack_version": "0.0.2-runtime-preview",
                "frontend_plugin": "adventurer_s1",
                "model_capabilities": [
                    "narrative_generation",
                    "structured_generation",
                    "semantic_review",
                ],
            },

        }

        created = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "版本化酒馆",
                "slug": "versioned-pack",
                "game_pack": "s1_fantasy",
            },
        )
        assert created.status_code == 201
        assert created.json["data"]["game_pack"] == "s1_fantasy"
        assert (
            created.json["data"]["game_pack_release"]
            == "0.0.2-runtime-preview"
        )
        context = client.get(
            "/api/taverns/versioned-pack/context",
            headers=admin_headers,
        )
        assert context.status_code == 200
        assert (
            context.json["data"]["runtime"]["frontend_plugin"]
            == "adventurer_s1"
        )
        assert (
            context.json["data"]["runtime"]["runtime_api_version"]
            == "0.1"
        )

        with app.app_context():
            tavern = db_manager.query(Tavern).filter_by(
                slug="versioned-pack"
            ).one()
            assert tavern.game_pack == "s1_fantasy"

    def test_admin_cannot_create_tavern_for_unknown_pack(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("unknown-pack-admin", is_admin=True)

        response = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "未知游戏",
                "slug": "unknown-game",
                "game_pack": "missing_pack",
            },
        )

        assert response.status_code == 400
        assert response.json["message"] == "游戏包不存在或与当前平台不兼容"

    def test_health_requires_matching_pack_and_runtime_api_version(
        self,
        app,
        client,
        make_user,
        monkeypatch,
    ):
        _, admin_headers = make_user("health-pack-admin", is_admin=True)
        created = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "健康契约", "slug": "health-contract"},
        )

        with app.app_context():
            tavern = db_manager.query(Tavern).filter_by(
                id=created.json["data"]["id"]
            ).one()

            monkeypatch.setattr(
                RuntimeSupervisorService,
                "_request_json",
                staticmethod(
                    lambda *args, **kwargs: (
                        {
                            "data": {
                                "runtime_id": tavern.runtime_namespace,
                                "game_pack": "wrong_pack",
                                "runtime_api_version": "0.1",
                                "pack_release": tavern.game_pack_release,
                            }
                        },
                        None,
                        200,
                    )
                ),
            )
            assert RuntimeSupervisorService._health(tavern) is False

            monkeypatch.setattr(
                RuntimeSupervisorService,
                "_request_json",
                staticmethod(
                    lambda *args, **kwargs: (
                        {
                            "data": {
                                "runtime_id": tavern.runtime_namespace,
                                "game_pack": "s1_fantasy",
                                "runtime_api_version": "0.1",
                                "pack_release": tavern.game_pack_release,
                            }
                        },
                        None,
                        200,
                    )
                ),
            )
            assert RuntimeSupervisorService._health(tavern) is True
