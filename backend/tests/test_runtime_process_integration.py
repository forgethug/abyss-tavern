import socket
from pathlib import Path

import pytest

from app import create_app
from config import TestConfig
from db_repository import db_manager
from models import Tavern
from services.runtime_supervisor_service import RuntimeSupervisorService


def _free_port_range(count: int) -> int:
    for first_port in range(42000, 61000 - count):
        probes = []
        try:
            for offset in range(count):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                probe.bind(("127.0.0.1", first_port + offset))
                probes.append(probe)
            return first_port
        except OSError:
            continue
        finally:
            for probe in probes:
                probe.close()
    raise RuntimeError(f"Could not find {count} consecutive free ports")


@pytest.mark.integration
def test_control_plane_starts_and_isolates_two_seeded_s1_runtimes(
    tmp_path: Path,
):
    first_runtime_port = _free_port_range(2)

    class RuntimeIntegrationConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = (
            f"sqlite:///{(tmp_path / 'control-plane.db').as_posix()}"
        )
        JWT_SECRET_KEY = "integration-secret-at-least-32-bytes"
        RUNTIME_AUTO_START = True
        RUNTIME_BASE_PORT = first_runtime_port - 1
        RUNTIME_DATA_DIR = str(tmp_path / "runtimes")
        RUNTIME_BOOTSTRAP_SECRET = "integration-bootstrap-secret-private-32"
        RUNTIME_START_TIMEOUT_SECONDS = 60

    app = create_app(RuntimeIntegrationConfig)
    client = app.test_client()
    runtime_tavern_ids = []
    try:
        register = client.post(
            "/api/auth/register",
            json={
                "username": "integration_player",
                "password": "pass1234",
                "display_name": "集成玩家",
            },
        )
        assert register.status_code == 201
        player_headers = {
            "Authorization": f"Bearer {register.json['data']['token']}"
        }

        with app.app_context():
            from models import User

            player = db_manager.query(User).filter_by(
                username="integration_player"
            ).one()
            player.is_admin = True
            ok, error = db_manager.commit()
            assert ok, error

        first_tavern = client.post(
            "/api/admin/taverns",
            headers=player_headers,
            json={
                "display_name": "星潮旅店",
                "slug": "star-tide",
            },
        )
        assert first_tavern.status_code == 201
        runtime_tavern_ids.append(first_tavern.json["data"]["id"])
        second_tavern = client.post(
            "/api/admin/taverns",
            headers=player_headers,
            json={
                "display_name": "余烬门厅",
                "slug": "ember-hall",
            },
        )
        assert second_tavern.status_code == 201
        runtime_tavern_ids.append(second_tavern.json["data"]["id"])

        for slug in ("star-tide", "ember-hall"):
            context = client.get(
                f"/api/taverns/{slug}/context",
                headers=player_headers,
            )
            assert context.status_code == 200, context.json
            assert (
                context.json["data"]["runtime"]["migration_status"]
                == "ready"
            )

        names_by_slug = {
            "star-tide": "星潮试炼者",
            "ember-hall": "余烬试炼者",
        }
        for slug, adventurer_name in names_by_slug.items():
            health = client.get(
                f"/api/taverns/{slug}/runtime/info",
                headers=player_headers,
            )
            assert health.status_code == 200
            assert health.json["data"]["status"] == "ok"

            options = client.get(
                f"/api/taverns/{slug}/runtime/creation-options",
                headers=player_headers,
            )
            assert options.status_code == 200
            assert len(options.json["data"]["skills"]) >= 16
            assert len(options.json["data"]["items"]) >= 17

            reservation = client.post(
                f"/api/taverns/{slug}/runtime/reserve",
                headers=player_headers,
                json={
                    "name": adventurer_name,
                    "gender": "女",
                    "personality": "勇毅",
                    "skill_template_id": options.json["data"]["skills"][0][
                        "id"
                    ],
                    "item_ids": [
                        options.json["data"]["items"][0]["id"],
                        options.json["data"]["items"][5]["id"],
                    ],
                },
            )
            assert reservation.status_code == 201, reservation.json
            draw = client.post(
                f"/api/taverns/{slug}/runtime/admin/draw",
                headers=player_headers,
                json={"count": 1},
            )
            assert draw.status_code == 200, draw.json

        first_list = client.get(
            "/api/taverns/star-tide/runtime/list",
            headers=player_headers,
        )
        second_list = client.get(
            "/api/taverns/ember-hall/runtime/list",
            headers=player_headers,
        )
        assert first_list.status_code == 200
        assert second_list.status_code == 200
        assert [row["name"] for row in first_list.json["data"]] == [
            "星潮试炼者"
        ]
        assert [row["name"] for row in second_list.json["data"]] == [
            "余烬试炼者"
        ]

        stop_response = client.post(
            f"/api/admin/taverns/{runtime_tavern_ids[0]}/runtime/stop",
            headers=player_headers,
        )
        assert stop_response.status_code == 200, stop_response.json
        assert stop_response.json["data"]["runtime_status"] == "stopped"
        assert stop_response.json["data"]["runtime_pid"] is None

        start_response = client.post(
            f"/api/admin/taverns/{runtime_tavern_ids[0]}/runtime/start",
            headers=player_headers,
        )
        assert start_response.status_code == 200, start_response.json
        assert start_response.json["data"]["runtime_status"] == "ready"
        started_pid = start_response.json["data"]["runtime_pid"]
        assert started_pid

        # Simulate a control-plane restart that lost its Popen handles, plus
        # a stale/missing stored launcher PID. The serving child must stop too.
        with app.app_context():
            first = db_manager.query(Tavern).filter_by(id=runtime_tavern_ids[0]).one()
            second = db_manager.query(Tavern).filter_by(id=runtime_tavern_ids[1]).one()
            old_first_processes = RuntimeSupervisorService._owned_runtime_processes(first)
            second_processes = RuntimeSupervisorService._owned_runtime_processes(second)
            assert old_first_processes and second_processes
            RuntimeSupervisorService._processes.pop(first.runtime_namespace, None)
            first.runtime_pid = None
            ok, error = db_manager.commit()
            assert ok, error

        restart_response = client.post(
            f"/api/admin/taverns/{runtime_tavern_ids[0]}/runtime/restart",
            headers=player_headers,
        )
        assert restart_response.status_code == 200, restart_response.json
        assert restart_response.json["data"]["runtime_status"] == "ready"
        assert restart_response.json["data"]["runtime_pid"]
        assert restart_response.json["data"]["runtime_pid"] != started_pid
        assert all(not process.is_running() for process in old_first_processes)
        assert all(process.is_running() for process in second_processes)

        persisted_first_list = client.get(
            "/api/taverns/star-tide/runtime/list",
            headers=player_headers,
        )
        assert persisted_first_list.status_code == 200
        assert [
            row["name"] for row in persisted_first_list.json["data"]
        ] == ["星潮试炼者"]

        with app.app_context():
            runtime_taverns = (
                db_manager.query(Tavern)
                .filter(Tavern.id.in_(runtime_tavern_ids))
                .order_by(Tavern.id.asc())
                .all()
            )
            assert len(runtime_taverns) == 2
            assert runtime_taverns[0].runtime_pid
            assert runtime_taverns[1].runtime_pid
            assert (
                runtime_taverns[0].runtime_pid
                != runtime_taverns[1].runtime_pid
            )
            assert (
                runtime_taverns[0].runtime_namespace
                != runtime_taverns[1].runtime_namespace
            )
            runtime_data_dir = Path(
                RuntimeIntegrationConfig.RUNTIME_DATA_DIR
            )
            assert (
                runtime_data_dir
                / f"{runtime_taverns[0].runtime_namespace}.db"
            ).is_file()
            assert (
                runtime_data_dir
                / f"{runtime_taverns[1].runtime_namespace}.db"
            ).is_file()
    finally:
        with app.app_context():
            for runtime_tavern_id in runtime_tavern_ids:
                attached = db_manager.query(Tavern).filter_by(
                    id=runtime_tavern_id
                ).one_or_none()
                if attached is not None:
                    RuntimeSupervisorService.stop_runtime(attached)
