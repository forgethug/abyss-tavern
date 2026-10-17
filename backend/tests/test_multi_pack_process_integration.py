import re
import socket
import sqlite3
from pathlib import Path

import pytest

from app import create_app
from config import TestConfig
from db_repository import db_manager
from models import Tavern
from services.runtime_supervisor_service import RuntimeSupervisorService


def _startup_diagnostics(directory):
    # Test failures expose only fixed phase markers, never application logs.
    marker = re.compile(
        r"\[runtime startup\] (?:imports|application|configure_pack|shared_schema|"
        r"pack_schema|seed|workers|scheduler|http) "
        r"(?:started|starting|failed|completed [0-9]+\.[0-9]{3}s)"
    )
    phases = []
    for path in sorted(Path(directory).glob("*.log")):
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        phases.extend(line for line in lines if marker.fullmatch(line))
    return phases[-32:]


def test_startup_diagnostics_excludes_runtime_data(tmp_path):
    (tmp_path / "runtime.log").write_text(
        "private configuration and player text\n"
        "[runtime startup] pack_schema started\n"
        "[runtime startup] pack_schema completed 1.234s\n"
        "[runtime startup] pack_schema failed secret-value\n",
        encoding="utf-8",
    )
    assert _startup_diagnostics(tmp_path) == [
        "[runtime startup] pack_schema started",
        "[runtime startup] pack_schema completed 1.234s",
    ]


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
def test_control_plane_runs_s1_and_second_pack_in_isolated_processes(
    tmp_path: Path,
    monkeypatch,
):
    # Explicit test-only registration; the application catalog remains S1-only.
    from services.game_pack_registry import GamePackRegistry
    definitions = GamePackRegistry.all()
    fixture = Path(__file__).resolve().parents[2] / 'runtime_host/tests/fixtures/framework-smoke/manifest.json'
    smoke = GamePackRegistry._load_manifest(fixture)
    definitions[smoke.pack_id] = smoke
    monkeypatch.setattr(GamePackRegistry, 'all', classmethod(lambda cls, catalog=definitions: catalog))
    first_runtime_port = _free_port_range(2)

    class MultiPackIntegrationConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = (
            f"sqlite:///{(tmp_path / 'control-plane.db').as_posix()}"
        )
        JWT_SECRET_KEY = "multi-pack-integration-secret-32-bytes"
        RUNTIME_AUTO_START = True
        RUNTIME_BASE_PORT = first_runtime_port - 1
        RUNTIME_DATA_DIR = str(tmp_path / "runtimes")
        RUNTIME_BOOTSTRAP_SECRET = "multi-pack-bootstrap-secret-private-32"
        RUNTIME_START_TIMEOUT_SECONDS = 40
        RUNTIME_SEED_ON_START = False
        RUNTIME_SCHEDULER_ENABLED = False
        RUNTIME_HEALTH_CACHE_SECONDS = 120

    app = create_app(MultiPackIntegrationConfig)
    client = app.test_client()
    tavern_ids = []
    process_ids = []
    process_handles = []
    runtime_namespaces = []
    try:
        register = client.post(
            "/api/auth/register",
            json={
                "username": "multi_pack_admin",
                "password": "pass1234",
                "display_name": "多游戏管理员",
            },
        )
        assert register.status_code == 201
        headers = {
            "Authorization": f"Bearer {register.json['data']['token']}"
        }

        with app.app_context():
            from models import User

            user = db_manager.query(User).filter_by(
                username="multi_pack_admin"
            ).one()
            user.is_admin = True
            ok, error = db_manager.commit()
            assert ok, error

        definitions = (
            ("s1-world", "S1 世界", "s1_fantasy"),
            ("smoke-world", "第二游戏", "framework_smoke"),
        )
        for slug, display_name, game_pack in definitions:
            created = client.post(
                "/api/admin/taverns",
                headers=headers,
                json={
                    "display_name": display_name,
                    "slug": slug,
                    "game_pack": game_pack,
                },
            )
            assert created.status_code == 201, created.json
            tavern_ids.append(created.json["data"]["id"])

            context = client.get(
                f"/api/taverns/{slug}/context",
                headers=headers,
            )
            assert context.status_code == 200, (
                f"pack={game_pack}, response={context.json}\n"
                + "\n".join(_startup_diagnostics(MultiPackIntegrationConfig.RUNTIME_DATA_DIR))
            )
            assert context.json["data"]["runtime"]["game_pack"] == game_pack
            assert context.json["data"]["runtime"]["runtime_api_version"] == "0.1"

        preparation_calls = []
        request_json = RuntimeSupervisorService._request_json

        def record_preparation(url, **kwargs):
            preparation_calls.append(url)
            return request_json(url, **kwargs)

        monkeypatch.setattr(RuntimeSupervisorService, "_request_json", staticmethod(record_preparation))
        s1_info = client.get(
            "/api/taverns/s1-world/runtime/info",
            headers=headers,
        )
        assert s1_info.status_code == 200
        assert s1_info.json["data"]["plugin"] == "adventurer_s1"

        smoke_info = client.get(
            "/api/taverns/smoke-world/runtime/info",
            headers=headers,
        )
        assert smoke_info.status_code == 200
        assert smoke_info.json["data"]["plugin"] == "framework_smoke"

        marker = client.post(
            "/api/taverns/smoke-world/runtime/markers",
            headers=headers,
            json={"label": "second-pack-only"},
        )
        assert marker.status_code == 201
        assert client.get(
            "/api/taverns/smoke-world/runtime/markers",
            headers=headers,
        ).json["data"] == [
            {"id": 1, "label": "second-pack-only"}
        ]
        assert (
            client.get(
                "/api/taverns/s1-world/runtime/markers",
                headers=headers,
            ).status_code
            == 404
        )
        # Both real child processes were verified and received the identity at
        # startup. Steady reads/writes now use only their business requests.
        assert preparation_calls == []

        s1_descriptor = client.get(
            (
                f"/api/admin/taverns/{tavern_ids[0]}/narrative/"
                "descriptor"
            ),
            headers=headers,
        )
        smoke_descriptor = client.get(
            (
                f"/api/admin/taverns/{tavern_ids[1]}/narrative/"
                "descriptor"
            ),
            headers=headers,
        )
        assert s1_descriptor.status_code == 200
        assert s1_descriptor.json["data"]["pack_id"] == "s1_fantasy"
        assert (
            s1_descriptor.json["data"]["supports_generic_writes"]
            is False
        )
        assert smoke_descriptor.status_code == 200
        assert (
            smoke_descriptor.json["data"]["pack_id"]
            == "framework_smoke"
        )

        candidate = client.post(
            (
                f"/api/admin/taverns/{tavern_ids[1]}/narrative/"
                "candidates"
            ),
            headers=headers,
            json={
                "idempotency_key": "multi-pack-world-update",
                "candidate_type": "world_update",
                "payload": {
                    "operations": [
                        {
                            "op": "upsert_entity",
                            "data": {
                                "external_key": "entity:smoke-only",
                                "kind": "anomaly",
                                "name": "仅第二游戏可见",
                            },
                        }
                    ]
                },
            },
        )
        assert candidate.status_code == 201, candidate.json
        accepted = client.post(
            (
                f"/api/admin/taverns/{tavern_ids[1]}/narrative/"
                f"candidates/{candidate.json['data']['candidate']['id']}"
                "/accept"
            ),
            headers=headers,
        )
        assert accepted.status_code == 200, accepted.json
        smoke_entities = client.get(
            (
                f"/api/admin/taverns/{tavern_ids[1]}/narrative/"
                "entities"
            ),
            headers=headers,
        )
        s1_entities = client.get(
            (
                f"/api/admin/taverns/{tavern_ids[0]}/narrative/"
                "entities"
            ),
            headers=headers,
        )
        assert smoke_entities.json["data"]["total"] == 1
        assert smoke_entities.json["data"]["items"][0]["name"] == (
            "仅第二游戏可见"
        )
        assert s1_entities.json["data"]["total"] == 0

        with app.app_context():
            taverns = (
                db_manager.query(Tavern)
                .filter(Tavern.id.in_(tavern_ids))
                .order_by(Tavern.id.asc())
                .all()
            )
            assert {tavern.game_pack for tavern in taverns} == {
                "s1_fantasy",
                "framework_smoke",
            }
            assert len({tavern.runtime_namespace for tavern in taverns}) == 2
            process_ids = [tavern.runtime_pid for tavern in taverns]
            runtime_namespaces = [
                tavern.runtime_namespace
                for tavern in taverns
            ]
            process_handles = [
                RuntimeSupervisorService._processes[tavern.runtime_namespace]
                for tavern in taverns
            ]
            assert all(process_ids)
            assert len(set(process_ids)) == 2
            for tavern in taverns:
                health, error, status = (
                    RuntimeSupervisorService._request_json(
                        f"{RuntimeSupervisorService.runtime_base_url(tavern)}"
                        "/api/runtime/health",
                    )
                )
                assert status == 200, error
                expected_pack_tables = (
                    1 if tavern.game_pack == "framework_smoke" else 142
                )
                assert (
                    health["data"]["pack_model_table_count"]
                    == expected_pack_tables
                )
                database_path = (
                    Path(MultiPackIntegrationConfig.RUNTIME_DATA_DIR)
                    / f"{tavern.runtime_namespace}.db"
                )
                assert database_path.is_file()
                with sqlite3.connect(database_path) as connection:
                    table_names = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table'"
                        )
                    }
                if tavern.game_pack == "s1_fantasy":
                    assert "adventurer_s1" in table_names
                    assert "framework_smoke_markers" not in table_names
                else:
                    assert "framework_smoke_markers" in table_names
                    assert not any(
                        name.startswith("adventurer_s1_")
                        for name in table_names
                    )
                assert "runtime_world_entities" in table_names
                with sqlite3.connect(database_path) as connection:
                    shared_entity_count = connection.execute(
                        "SELECT COUNT(*) FROM runtime_world_entities"
                    ).fetchone()[0]
                assert shared_entity_count == (
                    1
                    if tavern.game_pack == "framework_smoke"
                    else 0
                )

        for tavern_id in tavern_ids:
            stopped = client.post(
                f"/api/admin/taverns/{tavern_id}/runtime/stop",
                headers=headers,
            )
            assert stopped.status_code == 200, stopped.json
            assert stopped.json["data"]["runtime_pid"] is None
            assert stopped.json["data"]["runtime_status"] == "stopped"
        assert all(process.poll() is not None for process in process_handles)
        assert all(
            namespace not in RuntimeSupervisorService._processes
            for namespace in runtime_namespaces
        )
    finally:
        with app.app_context():
            for tavern_id in tavern_ids:
                tavern = db_manager.query(Tavern).filter_by(
                    id=tavern_id
                ).one_or_none()
                if tavern is not None:
                    RuntimeSupervisorService.stop_runtime(tavern)
