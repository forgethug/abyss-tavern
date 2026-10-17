import hmac
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request
from dotenv import load_dotenv

from paths import install_runtime_import_paths


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
install_runtime_import_paths()

from db_repository import db_manager
from extensions import db
from models.narrative import (
    RuntimeInjectionSnapshot,
    RuntimeNarrativeCandidate,
    RuntimeNarrativeLease,
    RuntimeNarrativeRevision,
    RuntimeStoryThread,
    RuntimeWorldEntity,
    RuntimeWorldFact,
)
from models.runtime_notification import RuntimeNotification
from models.system import SystemConfig
from models.user import User
from runtime_core.pack_adapter import load_runtime_pack_adapter
from runtime_core.model_gateway import ModelGateway
from runtime_core.narrative import NarrativePolicyRegistry
from runtime_core.task_registry import runtime_task_registry
from runtime_core.secrets import validate_secret
from runtime_core.schema import create_owned_tables
from runtime_core.time import utc_now
from startup import startup_phase


def create_runtime_app(
    database_uri: str = None,
    runtime_id: str = None,
    bootstrap_secret: str = None,
    seed_on_start: bool = None,
    adapter_path: str = None,
) -> Flask:
    app = Flask(__name__)
    runtime_id = runtime_id or os.getenv(
        "GAME_RUNTIME_ID",
        "local-runtime",
    )
    game_pack = os.getenv("GAME_RUNTIME_PACK_ID", "").strip()
    pack_release = os.getenv("GAME_RUNTIME_PACK_RELEASE", "").strip()
    runtime_api_version = os.getenv("GAME_RUNTIME_API_VERSION", "").strip()
    if not game_pack or not pack_release or not runtime_api_version:
        raise RuntimeError(
            "GAME_RUNTIME_PACK_ID, GAME_RUNTIME_PACK_RELEASE and "
            "GAME_RUNTIME_API_VERSION are required"
        )
    adapter_path = adapter_path or os.getenv("GAME_RUNTIME_ADAPTER_PATH")
    if not adapter_path:
        raise RuntimeError("GAME_RUNTIME_ADAPTER_PATH is required")
    adapter = load_runtime_pack_adapter(Path(adapter_path))
    if adapter.pack_id != game_pack:
        raise RuntimeError(
            f"Runtime adapter pack mismatch: expected {game_pack}, "
            f"got {adapter.pack_id}"
        )
    runtime_data_dir = Path(
        os.getenv(
            "GAME_RUNTIME_DATA_DIR",
            Path(__file__).resolve().parent / "data",
        )
    )
    runtime_data_dir.mkdir(parents=True, exist_ok=True)

    app.config.update(
        SECRET_KEY=os.getenv("GAME_RUNTIME_SECRET_KEY", ""),
        JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", ""),
        SQLALCHEMY_DATABASE_URI=database_uri
        or os.getenv(
            "GAME_RUNTIME_DATABASE_URL",
            f"sqlite:///{(runtime_data_dir / f'{runtime_id}.db').as_posix()}",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        GAME_RUNTIME_ID=runtime_id,
        GAME_RUNTIME_DATA_DIR=str(runtime_data_dir.resolve()),
        GAME_RUNTIME_PACK_ID=game_pack,
        GAME_RUNTIME_PACK_RELEASE=pack_release,
        GAME_RUNTIME_API_VERSION=runtime_api_version,
        GAME_RUNTIME_BOOTSTRAP_SECRET=bootstrap_secret
        or os.getenv(
            "GAME_RUNTIME_BOOTSTRAP_SECRET",
            "",
        ),
        GAME_RUNTIME_SEED_ON_START=(
            seed_on_start
            if seed_on_start is not None
            else os.getenv(
                "GAME_RUNTIME_SEED_ON_START",
                "true",
            ).lower()
            == "true"
        ),
    )
    for key in ('SECRET_KEY', 'JWT_SECRET_KEY', 'GAME_RUNTIME_BOOTSTRAP_SECRET'):
        validate_secret(key, app.config[key])
    db.init_app(app)
    shared_table_names = frozenset(
        {
            User.__tablename__,
            SystemConfig.__tablename__,
            RuntimeNotification.__tablename__,
            RuntimeWorldEntity.__tablename__,
            RuntimeWorldFact.__tablename__,
            RuntimeStoryThread.__tablename__,
            RuntimeNarrativeCandidate.__tablename__,
            RuntimeInjectionSnapshot.__tablename__,
            RuntimeNarrativeRevision.__tablename__,
            RuntimeNarrativeLease.__tablename__,
        }
    )
    app.extensions["game_pack_adapter"] = adapter
    app.extensions["model_gateway"] = ModelGateway.from_environment()
    runtime_task_registry.clear()
    with startup_phase("configure_pack"):
        adapter.configure_app(app)
    narrative_policy_registry = NarrativePolicyRegistry()
    adapter.register_narrative_policy(narrative_policy_registry)
    narrative_policy = narrative_policy_registry.create(game_pack, app)
    from routes.narrative import create_narrative_blueprint
    from services.narrative_engine import WorldNarrativeEngine

    narrative_engine = WorldNarrativeEngine(
        pack_id=game_pack,
        policy=narrative_policy,
    )
    app.extensions["narrative_policy_registry"] = (
        narrative_policy_registry
    )
    app.extensions["narrative_engine"] = narrative_engine
    app.register_blueprint(
        create_narrative_blueprint(narrative_engine)
    )
    pack_table_names = tuple(sorted(adapter.model_table_names(db)))
    missing_pack_tables = set(pack_table_names) - set(db.metadata.tables)
    if missing_pack_tables:
        raise RuntimeError(
            "Runtime adapter declared unknown model tables: "
            f"{sorted(missing_pack_tables)}"
        )
    if shared_table_names.intersection(pack_table_names):
        raise RuntimeError(
            "Runtime adapter attempted to own shared model tables"
        )
    app.extensions["game_pack_table_names"] = pack_table_names
    adapter.register_tasks(runtime_task_registry)
    from services.async_task_service import AsyncTaskService

    AsyncTaskService._refresh_supported_types()

    @app.get("/api/runtime/health")
    def runtime_health():
        data = {
            "plugin": adapter.plugin_name,
            "game_pack": game_pack,
            "runtime_api_version": runtime_api_version,
            "pack_release": pack_release,
            "status": "runtime_host_ready",
            "runtime_id": app.config["GAME_RUNTIME_ID"],
            "model_table_count": len(db.metadata.tables),
            "pack_model_table_count": len(pack_table_names),
            "model_gateway": app.extensions["model_gateway"].status(),
            "narrative_contract_version": (
                narrative_policy.contract_version
            ),
            "narrative_policy": narrative_engine.descriptor(),
            "game_route_count": sum(
                1
                for rule in app.url_map.iter_rules()
                if rule.rule.startswith(adapter.route_prefix)
            ),
        }
        data.update(adapter.health_metadata(app))
        return jsonify(
            {
                "code": 200,
                "message": "success",
                "data": data,
            }
        )

    @app.post("/api/runtime/identity/sync")
    def sync_identity():
        expected = app.config["GAME_RUNTIME_BOOTSTRAP_SECRET"]
        supplied = request.headers.get("X-Runtime-Bootstrap-Secret", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return jsonify({"code": 403, "message": "runtime bootstrap denied", "data": None}), 403

        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload["id"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"code": 400, "message": "invalid user id", "data": None}), 400
        username = str(payload.get("username") or "").strip()
        if not username:
            return jsonify({"code": 400, "message": "invalid username", "data": None}), 400

        user = db_manager.get(User, user_id)
        if user is None:
            user = User(id=user_id, username=username)
            ok, error = db_manager.add(user)
            if not ok:
                db_manager.rollback()
                return jsonify({"code": 500, "message": error, "data": None}), 500
        user.username = username
        user.nickname = str(payload.get("nickname") or "").strip() or None
        user.is_admin = bool(payload.get("is_admin", False))
        user.is_deleted = not bool(payload.get("is_active", True))
        raw_last_login = payload.get("last_login_at")
        if raw_last_login:
            try:
                user.last_login_at = datetime.fromisoformat(
                    str(raw_last_login).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except ValueError:
                return jsonify(
                    {
                        "code": 400,
                        "message": "invalid last_login_at",
                        "data": None,
                    }
                ), 400
        elif not user.is_deleted:
            user.last_login_at = utc_now()
        ok, error = db_manager.commit()
        if not ok:
            return jsonify({"code": 500, "message": error, "data": None}), 500
        return jsonify({"code": 200, "message": "identity synced", "data": user.to_dict()})

    with app.app_context():
        with startup_phase("shared_schema"):
            create_owned_tables(db, sorted(shared_table_names))
        with startup_phase("pack_schema"):
            adapter.migrate_database(db, pack_table_names)
        adapter.verify_models(db)
        assert SystemConfig.__tablename__ in db.metadata.tables
        assert RuntimeNotification.__tablename__ in db.metadata.tables
        if app.config["GAME_RUNTIME_SEED_ON_START"]:
            with startup_phase("seed"):
                adapter.seed()

    return app
