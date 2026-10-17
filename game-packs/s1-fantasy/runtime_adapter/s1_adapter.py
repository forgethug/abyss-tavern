import os

from runtime_core.model_gateway import RequestsTransportFacade
from runtime_core.schema import create_owned_tables
from runtime_core.task_registry import (
    dedupe_by_fields,
    global_dedupe_key,
)

_S1_MODEL_TABLE_NAMES = ()


class S1FantasyRuntimeAdapter:
    pack_id = "s1_fantasy"
    plugin_name = "adventurer_s1"
    route_prefix = "/api/adventurer-s1"

    def __init__(self):
        self._model_table_names = ()

    _TASK_DEDUPE_FIELDS = {
        "ai_s1_expedition": ("party_id", "expedition_log_id"),
        "ai_healing_house": ("adventurer_id", "usage_session_id"),
        "ai_blacksmith": ("adventurer_id", "usage_session_id"),
        "ai_tailor_workshop": ("adventurer_id", "usage_session_id"),
        "ai_alchemy_workshop": ("adventurer_id", "usage_session_id"),
        "ai_item_assembly_workshop": (
            "adventurer_id",
            "usage_session_id",
        ),
        "ai_ember_kitchen": ("adventurer_id", "usage_session_id"),
        "ai_chapel": ("adventurer_id", "usage_session_id"),
        "ai_skill_training": ("adventurer_id", "usage_session_id"),
        "ai_pet_evolution": ("adventurer_id", "usage_session_id"),
        "ai_s1_data_appeal": ("appeal_id",),
        "ai_s1_tavern_relief": ("appeal_id",),
        "ai_s1_tavern_activity": ("match_id",),
        "ai_s1_description_compress": ("batch_id",),
        "ai_s1_data_appeal_mimo_review": ("appeal_id",),
        "ai_s1_data_appeal_revert_request": ("target_appeal_id",),
        "ai_s1_expedition_observer_review": ("review_id",),
        "ai_s1_world_actor_profile_backfill": ("world_actor_id",),
        "ai_s1_fate_choices": ("adventurer_id", "generation_token"),
        "ai_s1_commission_refresh_one": ("commission_id",),
        "ai_s1_trait_recovery": ("expedition_log_id",),
        "ai_s1_desc_backfill": ("expedition_log_id",),
    }
    _GLOBAL_TASKS = {
        "ai_s1_world_outline_maintenance",
        "ai_s1_commission_refill",
        "ai_s1_commission_refresh",
        "ai_s1_trait_compress",
        "ai_s1_clue_bureau_supplement",
        "ai_s1_campaign_evolution",
    }

    @staticmethod
    def _environment_config() -> dict:
        fallback_raw = os.getenv(
            "ADVENTURER_S1_MIMO_USE_DEEPSEEK_FALLBACK",
            "false",
        )
        shared_profiles = os.getenv("GAME_RUNTIME_MODEL_PROFILES_JSON", "")
        shared_assignments = os.getenv(
            "GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON",
            "",
        )
        shared_reasoning = os.getenv(
            "GAME_RUNTIME_MODEL_REASONING_JSON",
            "",
        )
        profiles_config = (
            shared_profiles
            if shared_profiles not in {"", "[]"}
            else os.getenv("ADVENTURER_S1_AI_PROFILES_JSON", "")
        )
        assignments_config = (
            shared_assignments
            if shared_assignments not in {"", "{}"}
            else os.getenv(
                "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON",
                "",
            )
        )
        return {
            "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
            "DEEPSEEK_API_BASE": os.getenv("DEEPSEEK_API_BASE", ""),
            "DEEPSEEK_MODEL": os.getenv(
                "DEEPSEEK_MODEL",
                "deepseek-v4-pro",
            ),
            "ADVENTURER_S1_AI_PROVIDER": os.getenv(
                "ADVENTURER_S1_AI_PROVIDER",
                "deepseek",
            ),
            "ADVENTURER_S1_AI_PROFILES_JSON": profiles_config,
            "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON": assignments_config,
            "ADVENTURER_S1_AI_FEATURE_REASONING_JSON": (
                shared_reasoning
                if shared_reasoning not in {"", "{}"}
                else os.getenv(
                    "ADVENTURER_S1_AI_FEATURE_REASONING_JSON",
                    "",
                )
            ),
            "ADVENTURER_S1_AI_DEFAULT_PROFILE_ID": os.getenv(
                "ADVENTURER_S1_AI_DEFAULT_PROFILE_ID",
                "default",
            ),
            "MIMO_API_KEY": os.getenv("MIMO_API_KEY", ""),
            "MIMO_API_BASE": os.getenv(
                "MIMO_API_BASE",
                "https://token-plan-cn.xiaomimimo.com/v1",
            ),
            "MIMO_MODEL": os.getenv("MIMO_MODEL", "mimo-v2.5-pro"),
            "MIMO_TIMEOUT_SECONDS": int(
                os.getenv("MIMO_TIMEOUT_SECONDS", "600")
            ),
            "ADVENTURER_S1_MIMO_USE_DEEPSEEK_FALLBACK": (
                fallback_raw.lower() in {"1", "true", "yes", "on"}
            ),
        }

    def configure_app(self, app) -> None:
        global _S1_MODEL_TABLE_NAMES

        from extensions import db
        from runtime_core.pack_adapter import load_runtime_extension_factory

        tables_before_pack_import = set(db.metadata.tables)
        load_runtime_extension_factory(
            __file__, "server_compatibility.py", "install"
        )(app)
        from plugins.adventurer.plugin import AdventurerPlugin
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin
        from plugins.adventurer_s1.routes import adventurer_s1_bp
        from plugins.adventurer_s1.services import (
            s1_ai_runtime_config_service,
        )

        app.config.update(AdventurerPlugin.default_config)
        app.config.update(AdventurerS1Plugin.default_config)
        rules_config = load_runtime_extension_factory(
            __file__, "self_hosted_rules.py", "configuration"
        )
        app.config.update(rules_config())
        app.config.update(self._environment_config())
        load_runtime_extension_factory(__file__, "ai_input_mode.py", "install")(app)
        s1_ai_runtime_config_service.requests = RequestsTransportFacade(
            app.extensions["model_gateway"]
        )
        # Current upstream uses an app-scoped Session transport. Redirect that
        # entry point too, preserving its UTF-8 serialization and request options.
        s1_ai_runtime_config_service._app_s1_ai_http_transport = self._model_transport
        app.register_blueprint(adventurer_s1_bp)
        from routes.model_config import create_model_config_blueprint
        app.register_blueprint(create_model_config_blueprint(self.route_prefix))
        load_runtime_extension_factory(__file__, "ai_input_mode.py", "install_routes")(app)
        imported_table_names = tuple(
            sorted(set(db.metadata.tables) - tables_before_pack_import)
        )
        if imported_table_names:
            _S1_MODEL_TABLE_NAMES = imported_table_names
        self._model_table_names = _S1_MODEL_TABLE_NAMES
        if not self._model_table_names:
            raise RuntimeError("S1 adapter could not determine model ownership")

    def model_table_names(self, _db) -> tuple[str, ...]:
        return self._model_table_names

    @staticmethod
    def _model_transport():
        from flask import current_app

        return RequestsTransportFacade(current_app.extensions["model_gateway"])

    def register_tasks(self, registry) -> None:
        for task_type, fields in self._TASK_DEDUPE_FIELDS.items():
            registry.register(
                task_type,
                self._handler(task_type),
                dedupe_key_builder=dedupe_by_fields(task_type, *fields),
            )
        for task_type in self._GLOBAL_TASKS:
            registry.register(
                task_type,
                self._handler(task_type),
                dedupe_key_builder=global_dedupe_key(task_type),
            )

    @staticmethod
    def register_narrative_policy(registry) -> None:
        from runtime_core.pack_adapter import load_runtime_extension_factory

        registry.register(
            "s1_fantasy",
            load_runtime_extension_factory(
                __file__,
                "narrative_policy.py",
                "create_policy",
            ),
        )

    @staticmethod
    def migrate_database(db, table_names) -> None:
        create_owned_tables(db, table_names)
        from runtime_core.pack_adapter import load_runtime_extension_factory

        load_runtime_extension_factory(
            __file__, "public_content_migration.py", "migrate"
        )()
        from services.tavern_ai_input_settings import initialize
        initialize()

    def _handler(self, task_type):
        return lambda payload: self._dispatch(task_type, payload)

    def verify_models(self, db) -> None:
        from plugins.adventurer_s1.models import AdventurerS1

        assert AdventurerS1.__tablename__ in db.metadata.tables

    @staticmethod
    def seed() -> None:
        from plugins.adventurer_s1.seed import seed_all

        seed_all()

    @staticmethod
    def get_scheduler_tasks() -> list:
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin

        # Portable taverns need neither fixed-realm admission reconciliation
        # nor public-server gain monitoring / inactive-player clearance reminders.
        omitted = {
            "adventurer_s1_active_slot_reconcile",
            "adventurer_s1_gain_anomaly_monitor",
            "adventurer_s1_inactive_admin_digest",
        }
        return [
            task for task in AdventurerS1Plugin().get_scheduler_tasks()
            if task["id"] not in omitted
        ]

    @staticmethod
    def after_async_task_init(app) -> None:
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin

        AdventurerS1Plugin().after_async_task_init(app)

    def health_metadata(self, app) -> dict:
        return {
            "s1_ruleset": app.config["ADVENTURER_S1_RULESET"],
            "s1_route_count": sum(
                1
                for rule in app.url_map.iter_rules()
                if rule.rule.startswith(self.route_prefix)
            )
        }

    @staticmethod
    def _dispatch(task_type, payload):
        if task_type == "ai_s1_expedition":
            from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
                _do_s1_expedition_ai,
            )

            _do_s1_expedition_ai(
                int(payload["party_id"]),
                int(payload["expedition_log_id"]),
                force_retry=bool(payload.get("force_retry")),
            )
        elif task_type in {
            "ai_healing_house",
            "ai_blacksmith",
            "ai_tailor_workshop",
            "ai_alchemy_workshop",
            "ai_item_assembly_workshop",
            "ai_ember_kitchen",
            "ai_chapel",
            "ai_skill_training",
            "ai_pet_evolution",
        }:
            S1FantasyRuntimeAdapter._dispatch_building_ai(
                task_type,
                payload,
            )
        elif task_type == "ai_s1_tavern_activity":
            from plugins.adventurer_s1.services.s1_tavern_activity_service import (
                S1TavernActivityService,
            )

            S1TavernActivityService.process_match_ai(int(payload["match_id"]))
        elif task_type == "ai_s1_tavern_relief":
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
                AdventurerS1DataAppealService,
            )

            AdventurerS1DataAppealService.process_tavern_relief(
                int(payload["appeal_id"]),
                invoke_reason=str(payload.get("invoke_reason") or "unknown"),
                requeue_cause=payload.get("requeue_cause"),
                restart_recovered=bool(payload.get("restart_recovered")),
            )
        elif task_type == "ai_s1_description_compress":
            from plugins.adventurer_s1.services.s1_description_ai_service import (
                process_description_compression_task,
            )

            process_description_compression_task(
                payload, restart_recovered=bool(payload.get("restart_recovered")),
            )
        elif task_type == "ai_s1_campaign_evolution":
            from plugins.adventurer_s1.services.s1_campaign_evolution_service import (
                S1CampaignEvolutionService,
            )

            S1CampaignEvolutionService.process_pending()
        elif task_type == "ai_s1_data_appeal":
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
                AdventurerS1DataAppealService,
            )

            AdventurerS1DataAppealService.process_ai(
                int(payload["appeal_id"]),
                invoke_reason=str(payload.get("invoke_reason") or "unknown"),
                requeue_cause=payload.get("requeue_cause"),
                resume_stage=payload.get("resume_stage"),
                restart_recovered=bool(payload.get("restart_recovered")),
            )
        elif task_type == "ai_s1_data_appeal_mimo_review":
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
                AdventurerS1DataAppealService,
            )

            AdventurerS1DataAppealService.process_player_mimo_review(
                int(payload["appeal_id"])
            )
        elif task_type == "ai_s1_data_appeal_revert_request":
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
                AdventurerS1DataAppealService,
            )

            AdventurerS1DataAppealService.process_player_revert_request(
                int(payload["target_appeal_id"]),
                int(payload["stub_id"]) if payload.get("stub_id") else None,
            )
        elif task_type == "ai_s1_expedition_observer_review":
            from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import (
                process_expedition_observer_review,
            )

            process_expedition_observer_review(
                int(payload["review_id"]),
                restart_recovered=bool(payload.get("restart_recovered")),
            )
        elif task_type == "ai_s1_world_outline_maintenance":
            from plugins.adventurer_s1.services.s1_world_outline_service import (
                S1WorldOutlineService,
            )

            S1WorldOutlineService.process_pending_candidates()
        elif task_type == "ai_s1_world_actor_profile_backfill":
            from plugins.adventurer_s1.services.s1_world_actor_profile_backfill_ai_service import (
                run_world_actor_profile_backfill_ai,
            )

            run_world_actor_profile_backfill_ai(
                int(payload["world_actor_id"])
            )
        elif task_type == "ai_s1_fate_choices":
            from plugins.adventurer_s1.services.s1_fate_journey_service import (
                S1FateJourneyService,
            )

            S1FateJourneyService.generate_choices(
                int(payload["adventurer_id"]),
                int(payload["user_id"]),
                generation_token=payload.get("generation_token"),
            )
        elif task_type in {
            "ai_s1_commission_refill",
            "ai_s1_commission_refresh",
        }:
            from plugins.adventurer_s1.services.s1_commission_service import (
                S1CommissionService,
            )

            if task_type.endswith("refresh"):
                S1CommissionService.force_refresh()
            else:
                S1CommissionService.refill()
        elif task_type == "ai_s1_commission_refresh_one":
            from plugins.adventurer_s1.services.s1_commission_service import (
                S1CommissionService,
            )

            S1CommissionService.force_refresh_one(
                int(payload["commission_id"])
            )
        elif task_type == "ai_s1_trait_compress":
            from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
                S1TraitDescriptionAiService,
            )

            S1TraitDescriptionAiService.compress_admin(
                trait_ids=payload.get("trait_ids"),
                limit=payload.get("limit", 50),
                only_missing=payload.get("only_missing", True),
            )
        elif task_type == "ai_s1_clue_bureau_supplement":
            from plugins.adventurer_s1.services.s1_clue_bureau_service import (
                S1ClueBureauService,
            )

            S1ClueBureauService.supplement_trace_pool(
                target_count=payload.get("target_count"),
                updated_by_user_id=payload.get("updated_by_user_id"),
            )
        elif task_type == "ai_s1_trait_recovery":
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_ai_service import (
                run_s1_trait_recovery_ai,
            )

            run_s1_trait_recovery_ai(
                int(payload["expedition_log_id"]),
                party_id=(
                    int(payload["party_id"])
                    if payload.get("party_id")
                    else None
                ),
            )
        elif task_type == "ai_s1_desc_backfill":
            from plugins.adventurer_s1.services.adventurer_s1_desc_backfill_ai_service import (
                run_s1_desc_backfill_ai,
            )

            run_s1_desc_backfill_ai(int(payload["expedition_log_id"]))
        else:
            raise ValueError(f"Unsupported S1 async task type: {task_type}")

    @staticmethod
    def _dispatch_building_ai(task_type, payload):
        from plugins.adventurer_s1.services import s1_building_ai_service as ai

        adventurer_id = int(payload["adventurer_id"])
        building_id = int(payload["building_instance_id"])
        if task_type == "ai_healing_house":
            ai.call_healing_ai(
                adventurer_id,
                building_id,
                payload.get("target_traits"),
                item_ref=payload.get("item_ref"),
                usage_session_id=payload.get("usage_session_id"),
            )
        elif task_type == "ai_blacksmith":
            ai.call_blacksmith_ai(
                adventurer_id,
                building_id,
                payload.get("item_refs") or [],
                forge_intent=payload.get("forge_intent"),
                usage_session_id=payload.get("usage_session_id"),
            )
        elif task_type == "ai_tailor_workshop":
            ai.call_tailor_workshop_ai(
                adventurer_id, building_id, payload.get("item_refs") or [],
                tailor_operation=payload.get("tailor_operation"),
                usage_session_id=payload.get("usage_session_id"),
            )
        elif task_type == "ai_alchemy_workshop":
            ai.call_alchemy_workshop_ai(
                adventurer_id,
                building_id,
                payload.get("item_refs") or [],
                usage_session_id=payload.get("usage_session_id"),
            )
        elif task_type == "ai_item_assembly_workshop":
            ai.call_item_assembly_workshop_ai(
                adventurer_id,
                building_id,
                payload.get("item_refs") or [],
                layout_mode=payload.get("layout_mode"),
                layout_slots=payload.get("layout_slots"),
                desired_direction=payload.get("desired_direction"),
                usage_session_id=payload.get("usage_session_id"),
                item_snapshots=payload.get("item_snapshots"),
                input_slots=payload.get("input_slots"),
                input_snapshot_version=payload.get("input_snapshot_version"),
                input_snapshot_hash=payload.get("input_snapshot_hash"),
            )
        elif task_type == "ai_ember_kitchen":
            ai.call_ember_kitchen_ai(
                adventurer_id, building_id, payload.get("item_refs") or [],
                cookware_code=payload.get("cookware_code"),
                usage_session_id=payload.get("usage_session_id"),
                item_snapshots=payload.get("item_snapshots"),
                input_slots=payload.get("input_slots"),
                input_snapshot_version=payload.get("input_snapshot_version"),
                input_snapshot_hash=payload.get("input_snapshot_hash"),
            )
        elif task_type == "ai_chapel":
            ai.call_chapel_ai(
                adventurer_id,
                building_id,
                payload.get("target_traits"),
                usage_session_id=payload.get("usage_session_id"),
            )
        elif task_type == "ai_skill_training":
            ai.call_skill_training_ai(
                adventurer_id,
                building_id,
                str(payload["usage_session_id"]),
            )
        elif task_type == "ai_pet_evolution":
            ai.call_pet_evolution_ai(
                adventurer_id,
                building_id,
                int(payload["companion_id"]),
                charged_gold_cost=float(
                    payload.get("charged_gold_cost") or 0
                ),
                auxiliary_material=payload.get("auxiliary_material"),
                evolution_stage_at_start=payload.get(
                    "evolution_stage_at_start"
                ),
                usage_session_id=payload.get("usage_session_id"),
            )


def create_runtime_adapter():
    return S1FantasyRuntimeAdapter()
