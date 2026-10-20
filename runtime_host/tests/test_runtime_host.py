from runtime_core.time import utc_now
import importlib
import json
from pathlib import Path
from datetime import datetime, timedelta

import jwt
from app import create_runtime_app


S1_ADAPTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "game-packs"
    / "s1-fantasy"
    / "runtime_adapter"
    / "s1_adapter.py"
)
SMOKE_ADAPTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime_host"
    / "tests"
    / "fixtures"
    / "framework-smoke"
    / "runtime_adapter"
    / "smoke_adapter.py"
)


class TestRuntimeHost:
    def test_shared_host_loads_a_second_pack_without_s1_routes(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("GAME_RUNTIME_PACK_ID", "framework_smoke")
        monkeypatch.setenv("GAME_RUNTIME_PACK_RELEASE", "0.1.0")
        app = create_runtime_app(
            database_uri="sqlite:///:memory:",
            runtime_id="framework-smoke-runtime",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=False,
            adapter_path=str(SMOKE_ADAPTER_PATH),
        )
        client = app.test_client()

        health = client.get("/api/runtime/health")
        assert health.status_code == 200
        assert health.json["data"]["game_pack"] == "framework_smoke"
        assert health.json["data"]["plugin"] == "framework_smoke"
        assert health.json["data"]["game_route_count"] == 3
        assert client.get("/api/framework-smoke/info").status_code == 200
        assert client.get("/api/adventurer-s1/info").status_code == 404

        created = client.post(
            "/api/framework-smoke/markers",
            json={"label": "isolated"},
        )
        assert created.status_code == 201
        assert client.get("/api/framework-smoke/markers").json["data"] == [
            {"id": 1, "label": "isolated"}
        ]

    def test_portable_s1_snapshot_modules_import_in_runtime_host(
        self,
        runtime_app,
    ):
        snapshot_backend = (
            Path(__file__).resolve().parents[2]
            / "game-packs"
            / "s1-fantasy"
            / "source-snapshot"
            / "backend"
        )
        package_root = snapshot_backend / "plugins" / "adventurer_s1"
        modules = []
        for source_path in package_root.rglob("*.py"):
            relative = source_path.relative_to(snapshot_backend).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            modules.append(".".join(parts))

        failures = []
        with runtime_app.app_context():
            for module_name in sorted(set(modules)):
                try:
                    importlib.import_module(module_name)
                except Exception as exc:
                    failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
        assert not failures, "\n".join(failures)

    def test_runtime_reads_ai_provider_environment(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
        monkeypatch.setenv("DEEPSEEK_API_BASE", "https://deepseek.test/v1")
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test-model")
        monkeypatch.setenv("MIMO_API_KEY", "mimo-test-key")
        monkeypatch.setenv("MIMO_MODEL", "mimo-test-model")
        monkeypatch.setenv(
            "ADVENTURER_S1_MIMO_USE_DEEPSEEK_FALLBACK",
            "true",
        )

        app = create_runtime_app(
            database_uri="sqlite:///:memory:",
            runtime_id="provider-config",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=False,
            adapter_path=str(S1_ADAPTER_PATH),
        )

        assert app.config["DEEPSEEK_API_KEY"] == "deepseek-test-key"
        assert app.config["DEEPSEEK_API_BASE"] == "https://deepseek.test/v1"
        assert app.config["DEEPSEEK_MODEL"] == "deepseek-test-model"
        assert app.config["MIMO_API_KEY"] == "mimo-test-key"
        assert app.config["MIMO_MODEL"] == "mimo-test-model"
        assert app.config["ADVENTURER_S1_MIMO_USE_DEEPSEEK_FALLBACK"] is True

    def test_s1_compatibility_adapter_reads_shared_model_profiles(
        self,
        monkeypatch,
    ):
        profiles = [
            {
                "id": "primary",
                "provider": "openai_compatible",
                "api_base": "https://models.test/v1",
                "api_key": "central-secret",
                "model": "story-model",
            }
        ]
        assignments = {
            "narrative_generation": "primary",
            "expedition_main": "primary",
        }
        monkeypatch.delenv("ADVENTURER_S1_AI_PROFILES_JSON", raising=False)
        monkeypatch.delenv(
            "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON",
            raising=False,
        )
        monkeypatch.setenv(
            "GAME_RUNTIME_MODEL_PROFILES_JSON",
            json.dumps(profiles),
        )
        monkeypatch.setenv(
            "GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON",
            json.dumps(assignments),
        )
        monkeypatch.setenv(
            "GAME_RUNTIME_MODEL_REASONING_JSON",
            '{"expedition_main":"high"}',
        )
        monkeypatch.setenv(
            "GAME_RUNTIME_MODEL_CAPABILITIES_JSON",
            '["narrative_generation"]',
        )
        monkeypatch.setenv(
            "ADVENTURER_S1_AI_PROFILES_JSON",
            '[{"id":"legacy","api_base":"https://legacy.invalid/v1",'
            '"model":"legacy"}]',
        )
        monkeypatch.setenv(
            "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON",
            '{"expedition_main":"legacy"}',
        )

        app = create_runtime_app(
            database_uri="sqlite:///:memory:",
            runtime_id="shared-model-config",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=False,
            adapter_path=str(S1_ADAPTER_PATH),
        )

        assert json.loads(
            app.config["ADVENTURER_S1_AI_PROFILES_JSON"]
        ) == profiles
        assert json.loads(
            app.config["ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON"]
        ) == assignments
        assert json.loads(
            app.config["ADVENTURER_S1_AI_FEATURE_REASONING_JSON"]
        ) == {"expedition_main": "high"}
        health = app.test_client().get("/api/runtime/health")
        assert health.json["data"]["model_gateway"] == {
            "declared_capabilities": ["narrative_generation"],
            "configured_capabilities": ["narrative_generation"],
        }

        calls = []
        app.extensions["model_gateway"]._post = (
            lambda *args, **kwargs: calls.append((args, kwargs))
            or "raw-response"
        )
        from plugins.adventurer_s1.services import (
            s1_ai_runtime_config_service,
        )

        with app.app_context():
            resolved = (
                s1_ai_runtime_config_service.resolve_s1_ai_profile_snapshot(
                    "expedition_main"
                )
            )
        assert resolved.profile_id == "primary"
        assert resolved.reasoning_effort == "high"

        assert (
            s1_ai_runtime_config_service.requests.post(
                "https://models.test/v1/chat/completions",
                timeout=3,
            )
            == "raw-response"
        )
        assert calls == [
            (
                ("https://models.test/v1/chat/completions",),
                {"timeout": 3},
            )
        ]

        # Exercise the actual current snapshot transport entry point; a plain
        # requests.post test misses upstream's pooled Session path.
        payload = {"model": "story-model", "messages": [{"role": "user", "content": "酒馆探险"}]}
        with app.app_context():
            assert s1_ai_runtime_config_service._post_s1_ai_http_request(
                "https://models.test/v1/chat/completions", json=payload, timeout=9,
            ) == "raw-response"
        assert len(calls) == 2
        args, kwargs = calls[-1]
        assert args == ("https://models.test/v1/chat/completions",)
        assert json.loads(kwargs["data"].decode("utf-8")) == payload
        assert "酒馆探险".encode("utf-8") in kwargs["data"]
        assert kwargs["headers"]["Content-Type"] == "application/json; charset=utf-8"
        assert kwargs["timeout"] == 9

    def test_async_dispatcher_covers_all_current_snapshot_task_types(self):
        from services.async_task_service import AsyncTaskService

        assert AsyncTaskService.SUPPORTED_TASK_TYPES == {
            "ai_s1_expedition",
            "ai_healing_house",
            "ai_blacksmith",
            "ai_tailor_workshop",
            "ai_alchemy_workshop",
            "ai_item_assembly_workshop",
            "ai_ember_kitchen",
            "ai_chapel",
            "ai_skill_training",
            "ai_pet_evolution",
            "ai_s1_data_appeal",
            "ai_s1_tavern_relief",
            "ai_s1_tavern_activity",
            "ai_s1_description_compress",
            "ai_s1_campaign_evolution",
            "ai_s1_data_appeal_mimo_review",
            "ai_s1_data_appeal_revert_request",
            "ai_s1_expedition_observer_review",
            "ai_s1_world_outline_maintenance",
            "ai_s1_world_actor_profile_backfill",
            "ai_s1_fate_choices",
            "ai_s1_commission_refill",
            "ai_s1_commission_refresh",
            "ai_s1_commission_refresh_one",
            "ai_s1_trait_compress",
            "ai_s1_clue_bureau_supplement",
            "ai_s1_trait_recovery",
            "ai_s1_desc_backfill",
        }

    def test_async_queue_rejects_unknown_task_type(self, runtime_app):
        from services.async_task_service import AsyncTaskService

        AsyncTaskService.init_app(runtime_app)
        assert AsyncTaskService.submit_task("ai_unknown", {}) is False

    def test_appeal_mailbox_alias_persists_runtime_notification(
        self,
        runtime_app,
    ):
        with runtime_app.app_context():
            from db_repository import db_manager
            from models.mailbox import Mailbox
            from models.runtime_notification import RuntimeNotification

            mail = Mailbox(
                user_id=71,
                title="申诉结果",
                content="已完成复核",
                sender_name="酒馆管理处",
                mail_type="s1_appeal_result",
                reference_id=12,
            )
            ok, error = db_manager.add(mail)
            assert ok, error
            ok, error = db_manager.commit()
            assert ok, error

            stored = RuntimeNotification.query.one()
            assert stored.user_id == 71
            assert stored.mail_type == "s1_appeal_result"
            assert stored.reference_id == 12

    def test_runtime_event_sink_accepts_s1_emit_calls(self):
        from extensions import socketio

        assert (
            socketio.emit(
                "adventurer_changed",
                {"ids": [1]},
                namespace="/",
            )
            is None
        )

    def test_loads_complete_s1_model_metadata(self, runtime_client):
        response = runtime_client.get("/api/runtime/health")
        assert response.status_code == 200
        assert response.json["data"]["status"] == "runtime_host_ready"
        assert response.json["data"]["model_table_count"] >= 90
        assert response.json["data"]["s1_route_count"] == 592
        assert response.json["data"]["s1_ruleset"] == "self_hosted"

    def test_exposes_source_snapshot_info_route(self, runtime_client):
        response = runtime_client.get("/api/adventurer-s1/info")
        assert response.status_code == 200
        assert response.json["data"] == {
            "plugin": "adventurer_s1",
            "status": "ok",
        }

    def test_registers_only_portable_s1_scheduler_definitions(self, runtime_app):
        from services.scheduler_service import SchedulerService

        SchedulerService.init_app(runtime_app)
        try:
            from plugins.adventurer_s1.plugin import AdventurerS1Plugin

            with runtime_app.app_context():
                upstream_jobs = AdventurerS1Plugin().get_scheduler_tasks()
            assert {job["id"] for job in SchedulerService._jobs} == {
                job["id"] for job in upstream_jobs
                if job["id"] not in {
                    "adventurer_s1_active_slot_reconcile",
                    "adventurer_s1_gain_anomaly_monitor",
                    "adventurer_s1_inactive_admin_digest",
                }
            }
            assert len(SchedulerService._jobs) == 38
            assert {
                job["id"] for job in SchedulerService._jobs
            } >= {
                "adventurer_s1_expedition_dispatch_tick",
                "adventurer_s1_expedition_progress_tick",
                "adventurer_s1_daily_reservation_draw",
                "adventurer_s1_tavern_activity_tick",
                "adventurer_s1_commission_refill_tick",
                "adventurer_s1_commission_daily_refresh",
            }
        finally:
            SchedulerService.stop()

    def test_async_queue_rejects_duplicate_s1_work_key(self, runtime_app):
        from services.async_task_service import AsyncTaskService

        payload = {"party_id": 7, "expedition_log_id": 9}
        key = AsyncTaskService._build_task_dedupe_key(
            "ai_s1_expedition",
            payload,
        )
        assert key == "ai_s1_expedition:7:9"
        AsyncTaskService._active_keys.add(key)
        try:
            assert (
                AsyncTaskService.is_task_enqueued_or_running(
                    "ai_s1_expedition",
                    payload,
                )
                is True
            )
            assert (
                AsyncTaskService.force_release_task_lock(
                    "ai_s1_expedition",
                    payload,
                )
                is True
            )
            assert (
                AsyncTaskService.is_task_enqueued_or_running(
                    "ai_s1_expedition",
                    payload,
                )
                is False
            )
        finally:
            AsyncTaskService._active_keys.discard(key)

    def test_identity_sync_requires_internal_secret(self, runtime_client):
        denied = runtime_client.post(
            "/api/runtime/identity/sync",
            json={"id": 7, "username": "alice"},
        )
        assert denied.status_code == 403

        accepted = runtime_client.post(
            "/api/runtime/identity/sync",
            headers={"X-Runtime-Bootstrap-Secret": "test-bootstrap-secret-private-32-bytes"},
            json={
                "id": 7,
                "username": "alice",
                "nickname": "爱丽丝",
                "is_admin": False,
                "is_active": True,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json["data"]["id"] == 7
        assert accepted.json["data"]["nickname"] == "爱丽丝"

    def test_authenticated_player_can_load_s1_init(self, runtime_client):
        accepted = runtime_client.post(
            "/api/runtime/identity/sync",
            headers={"X-Runtime-Bootstrap-Secret": "test-bootstrap-secret-private-32-bytes"},
            json={
                "id": 19,
                "username": "runtime-player",
                "nickname": "试跑玩家",
                "is_active": True,
            },
        )
        assert accepted.status_code == 200
        now = utc_now()
        token = jwt.encode(
            {
                "user_id": 19,
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            "runtime-jwt-test-private-secret-32-bytes",
            algorithm="HS256",
        )
        response = runtime_client.get(
            "/api/adventurer-s1/init",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json["code"] == 200
        assert isinstance(response.json["data"], dict)

    def test_two_runtime_databases_do_not_share_identity_or_s1_rows(self, tmp_path: Path):
        first_db = tmp_path / "first.db"
        second_db = tmp_path / "second.db"
        first = create_runtime_app(
            database_uri=f"sqlite:///{first_db.as_posix()}",
            runtime_id="first",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=False,
            adapter_path=str(S1_ADAPTER_PATH),
        )
        second = create_runtime_app(
            database_uri=f"sqlite:///{second_db.as_posix()}",
            runtime_id="second",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=False,
            adapter_path=str(S1_ADAPTER_PATH),
        )

        with first.app_context():
            from db_repository import db_manager
            from models.user import User
            from plugins.adventurer_s1.models import AdventurerS1

            user = User(id=11, username="first-player")
            ok, error = db_manager.add(user)
            assert ok, error
            adventurer = AdventurerS1(
                name="仅属于第一酒馆",
                gender="女",
                owner_id=11,
                is_player_created=True,
            )
            ok, error = db_manager.add(adventurer)
            assert ok, error
            ok, error = db_manager.commit()
            assert ok, error
            assert AdventurerS1.query.count() == 1

        with second.app_context():
            from models.user import User
            from plugins.adventurer_s1.models import AdventurerS1

            assert User.query.count() == 0
            assert AdventurerS1.query.count() == 0

    def test_seeded_runtime_contains_required_game_catalogs(self, tmp_path: Path):
        app = create_runtime_app(
            database_uri=f"sqlite:///{(tmp_path / 'seeded.db').as_posix()}",
            runtime_id="seeded",
            bootstrap_secret="runtime-test-bootstrap-secret-32-bytes",
            seed_on_start=True,
            adapter_path=str(S1_ADAPTER_PATH),
        )
        with app.app_context():
            from plugins.adventurer_s1.models import (
                AdventurerS1BuildingTemplate,
                AdventurerS1Item,
                AdventurerS1SkillTemplate,
                AdventurerS1TavernSeat,
            )

            assert AdventurerS1SkillTemplate.query.count() >= 16
            assert (
                AdventurerS1Item.query.filter_by(is_starter=True).count()
                >= 17
            )
            assert AdventurerS1BuildingTemplate.query.count() >= 49
            assert AdventurerS1TavernSeat.query.count() >= 18

        client = app.test_client()
        for user_id, username, is_admin in (
            (31, "player", False),
            (32, "admin", True),
        ):
            response = client.post(
                "/api/runtime/identity/sync",
                headers={"X-Runtime-Bootstrap-Secret": "runtime-test-bootstrap-secret-32-bytes"},
                json={
                    "id": user_id,
                    "username": username,
                    "is_admin": is_admin,
                    "is_active": True,
                },
            )
            assert response.status_code == 200

        now = utc_now()

        def auth_headers(user_id: int):
            token = jwt.encode(
                {
                    "user_id": user_id,
                    "iat": now,
                    "exp": now + timedelta(minutes=5),
                },
                "runtime-jwt-test-private-secret-32-bytes",
                algorithm="HS256",
            )
            return {"Authorization": f"Bearer {token}"}

        player_headers = auth_headers(31)
        options = client.get(
            "/api/adventurer-s1/creation-options",
            headers=player_headers,
        )
        assert options.status_code == 200
        assert len(options.json["data"]["skills"]) >= 16
        assert len(options.json["data"]["items"]) >= 17

        reservation = client.post(
            "/api/adventurer-s1/reserve",
            headers=player_headers,
            json={
                "name": "迁移试炼者",
                "gender": "女",
                "personality": "勇毅",
                "skill_template_id": options.json["data"]["skills"][0]["id"],
                "item_ids": [
                    options.json["data"]["items"][0]["id"],
                    options.json["data"]["items"][5]["id"],
                ],
            },
        )
        assert reservation.status_code == 201

        draw = client.post(
            "/api/adventurer-s1/admin/draw",
            headers=auth_headers(32),
            json={"count": 1},
        )
        assert draw.status_code == 200, draw.json
        adventurers = client.get(
            "/api/adventurer-s1/list",
            headers=player_headers,
        )
        assert adventurers.status_code == 200
        assert adventurers.json["data"], draw.json
        assert adventurers.json["data"][0]["name"] == "迁移试炼者"

        init_data = client.get(
            "/api/adventurer-s1/init",
            headers=player_headers,
        )
        assert init_data.status_code == 200, init_data.json
        assert init_data.json["data"]["adventurers"][0]["name"] == "迁移试炼者"
