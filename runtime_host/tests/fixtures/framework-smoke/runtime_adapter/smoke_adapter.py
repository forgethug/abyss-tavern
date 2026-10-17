from flask import Blueprint, jsonify, request

from extensions import db


class FrameworkSmokeMarker(db.Model):
    __tablename__ = "framework_smoke_markers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    label = db.Column(db.String(80), nullable=False)


framework_smoke_bp = Blueprint(
    "framework_smoke",
    __name__,
    url_prefix="/api/framework-smoke",
)


@framework_smoke_bp.get("/info")
def info():
    return jsonify(
        {
            "code": 200,
            "message": "success",
            "data": {
                "plugin": "framework_smoke",
                "status": "ok",
            },
        }
    )


@framework_smoke_bp.get("/markers")
def list_markers():
    rows = FrameworkSmokeMarker.query.order_by(
        FrameworkSmokeMarker.id.asc()
    ).all()
    return jsonify(
        {
            "code": 200,
            "message": "success",
            "data": [
                {"id": row.id, "label": row.label}
                for row in rows
            ],
        }
    )


@framework_smoke_bp.post("/markers")
def create_marker():
    payload = request.get_json(silent=True) or {}
    label = str(payload.get("label") or "").strip()
    if not label or len(label) > 80:
        return jsonify(
            {
                "code": 400,
                "message": "invalid marker label",
                "data": None,
            }
        ), 400
    marker = FrameworkSmokeMarker(label=label)
    db.session.add(marker)
    db.session.commit()
    return jsonify(
        {
            "code": 201,
            "message": "created",
            "data": {"id": marker.id, "label": marker.label},
        }
    ), 201


class FrameworkSmokeRuntimeAdapter:
    pack_id = "framework_smoke"
    plugin_name = "framework_smoke"
    route_prefix = "/api/framework-smoke"

    @staticmethod
    def configure_app(app) -> None:
        app.register_blueprint(framework_smoke_bp)

    @staticmethod
    def model_table_names(_db) -> tuple[str, ...]:
        return (FrameworkSmokeMarker.__tablename__,)

    @staticmethod
    def register_tasks(_registry) -> None:
        return None

    @staticmethod
    def register_narrative_policy(registry) -> None:
        from runtime_core.pack_adapter import load_runtime_extension_factory

        registry.register(
            "framework_smoke",
            load_runtime_extension_factory(
                __file__,
                "narrative_policy.py",
                "create_policy",
            ),
        )

    @staticmethod
    def migrate_database(db_instance, table_names) -> None:
        db_instance.metadata.create_all(
            bind=db_instance.engine,
            tables=[
                db_instance.metadata.tables[name]
                for name in table_names
            ],
        )

    @staticmethod
    def verify_models(db_instance) -> None:
        assert FrameworkSmokeMarker.__tablename__ in db_instance.metadata.tables

    @staticmethod
    def seed() -> None:
        return None

    @staticmethod
    def get_scheduler_tasks() -> list:
        return []

    @staticmethod
    def after_async_task_init(_app) -> None:
        return None

    @staticmethod
    def health_metadata(_app) -> dict:
        return {}


def create_runtime_adapter():
    return FrameworkSmokeRuntimeAdapter()
