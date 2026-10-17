from runtime_core.time import utc_now
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import jwt

from app import create_runtime_app


SMOKE_ADAPTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime_host"
    / "tests"
    / "fixtures"
    / "framework-smoke"
    / "runtime_adapter"
    / "smoke_adapter.py"
)


def _smoke_app(monkeypatch, database_uri="sqlite:///:memory:"):
    monkeypatch.setenv("GAME_RUNTIME_PACK_ID", "framework_smoke")
    monkeypatch.setenv("GAME_RUNTIME_PACK_RELEASE", "0.1.0")
    monkeypatch.setenv("GAME_RUNTIME_API_VERSION", "0.1")
    return create_runtime_app(
        database_uri=database_uri,
        runtime_id="narrative-smoke",
        bootstrap_secret="narrative-private-test-secret-32-bytes",
        seed_on_start=False,
        adapter_path=str(SMOKE_ADAPTER_PATH),
    )


def _auth(
    client,
    *,
    user_id=91,
    is_admin=True,
    bootstrap_secret="narrative-private-test-secret-32-bytes",
):
    response = client.post(
        "/api/runtime/identity/sync",
        headers={"X-Runtime-Bootstrap-Secret": bootstrap_secret},
        json={
            "id": user_id,
            "username": f"user-{user_id}",
            "is_admin": is_admin,
            "is_active": True,
        },
    )
    assert response.status_code == 200
    now = utc_now()
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


def _world_update(*operations):
    return {
        "idempotency_key": "candidate-1",
        "candidate_type": "world_update",
        "source_run_key": "run-1",
        "source_stage_key": "reviewed",
        "payload": {"operations": list(operations)},
    }


class TestNarrativeRuntime:
    def test_s1_policy_projects_existing_authority_without_writes(
        self,
        runtime_app,
        runtime_client,
    ):
        with runtime_app.app_context():
            from db_repository import db_manager
            from plugins.adventurer_s1.models import (
                AdventurerS1WorldActor,
                AdventurerS1WorldLocationState,
                AdventurerS1WorldStoryThread,
            )

            db_manager.add(
                AdventurerS1WorldActor(
                    canonical_name="旧王庭信使",
                    actor_type="individual",
                    identity_summary="跨副本持续存在",
                    scope_keys=["灰烬城"],
                    importance=77,
                )
            )
            db_manager.add(
                AdventurerS1WorldLocationState(
                    target_dungeon="灰烬城",
                    canonical_name="北门",
                    current_state="已封锁",
                    revision=3,
                )
            )
            db_manager.add(
                AdventurerS1WorldStoryThread(
                    title="灰烬城的余烬",
                    thread_type="event",
                    base_dungeon="灰烬城",
                    summary="火势并未真正熄灭",
                    weight=66,
                    revision=4,
                )
            )
            ok, error = db_manager.commit()
            assert ok, error

        headers = _auth(
            runtime_client,
            bootstrap_secret="test-bootstrap-secret-private-32-bytes",
        )
        descriptor = runtime_client.get(
            "/api/runtime/narrative/descriptor",
            headers=headers,
        )
        assert descriptor.status_code == 200
        assert descriptor.json["data"]["pack_id"] == "s1_fantasy"
        assert descriptor.json["data"]["supports_generic_writes"] is False

        entities = runtime_client.get(
            "/api/runtime/narrative/entities",
            headers=headers,
        ).json["data"]["items"]
        facts = runtime_client.get(
            "/api/runtime/narrative/facts",
            headers=headers,
        ).json["data"]["items"]
        threads = runtime_client.get(
            "/api/runtime/narrative/story-threads",
            headers=headers,
        ).json["data"]["items"]
        assert entities[0]["name"] == "旧王庭信使"
        assert entities[0]["source"] == "pack_compat"
        assert facts[0]["summary"] == "已封锁"
        assert threads[0]["title"] == "灰烬城的余烬"

        rejected = runtime_client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json={
                "idempotency_key": "s1-write",
                "candidate_type": "world_update",
                "payload": {"operations": []},
            },
        )
        assert rejected.status_code == 400
    def test_admin_auth_and_pack_descriptor(self, monkeypatch):
        client = _smoke_app(monkeypatch).test_client()
        assert (
            client.get("/api/runtime/narrative/descriptor").status_code
            == 401
        )
        player_headers = _auth(client, user_id=92, is_admin=False)
        assert (
            client.get(
                "/api/runtime/narrative/descriptor",
                headers=player_headers,
            ).status_code
            == 403
        )
        admin_headers = _auth(client)
        response = client.get(
            "/api/runtime/narrative/descriptor",
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json["data"]["pack_id"] == "framework_smoke"
        assert response.json["data"]["candidate_types"] == [
            "world_update"
        ]

    def test_candidate_is_normalized_idempotent_and_applied_once(
        self,
        monkeypatch,
    ):
        app = _smoke_app(monkeypatch)
        client = app.test_client()
        headers = _auth(client)
        payload = _world_update(
            {
                "op": "upsert_entity",
                "ignored": "raw-model-noise",
                "data": {
                    "external_key": "entity:warden",
                    "kind": "personnel",
                    "name": "值班主管",
                    "summary": "只记录审核后的投影",
                    "visibility": "members",
                    "scope_keys": ["site-a"],
                    "attributes": {"clearance": 1},
                    "ignored": "must-not-persist",
                },
            },
            {
                "op": "upsert_fact",
                "data": {
                    "fact_key": "fact:red-door",
                    "kind": "rule",
                    "subject_key": "entity:warden",
                    "predicate": "requires",
                    "value": "不要回应第三次敲门",
                    "summary": "红门规则",
                    "visibility": "public",
                    "scope_keys": ["site-a"],
                },
            },
        )
        created = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=payload,
        )
        assert created.status_code == 201
        candidate = created.json["data"]["candidate"]
        assert candidate["normalized_payload"]["operations"][0] == {
            "op": "upsert_entity",
            "data": {
                "external_key": "entity:warden",
                "kind": "personnel",
                "name": "值班主管",
                "summary": "只记录审核后的投影",
                "visibility": "members",
                "scope_keys": ["site-a"],
                "attributes": {"clearance": 1},
            },
        }

        repeated = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=payload,
        )
        assert repeated.status_code == 200
        assert repeated.json["data"]["created"] is False

        accepted = client.post(
            f"/api/runtime/narrative/candidates/{candidate['id']}/accept",
            headers=headers,
        )
        assert accepted.status_code == 200
        assert accepted.json["data"]["applied"] is True
        accepted_again = client.post(
            f"/api/runtime/narrative/candidates/{candidate['id']}/accept",
            headers=headers,
        )
        assert accepted_again.status_code == 200
        assert accepted_again.json["data"]["applied"] is False

        with app.app_context():
            from models.narrative import (
                RuntimeNarrativeRevision,
                RuntimeWorldEntity,
                RuntimeWorldFact,
            )

            assert RuntimeWorldEntity.query.count() == 1
            assert RuntimeWorldFact.query.count() == 1
            assert RuntimeNarrativeRevision.query.count() == 2
            assert {
                row.source_ref
                for row in RuntimeNarrativeRevision.query.all()
            } == {f"candidate:{candidate['id']}"}

    def test_accept_rolls_back_all_operations_on_validation_failure(
        self,
        monkeypatch,
    ):
        app = _smoke_app(monkeypatch)
        client = app.test_client()
        headers = _auth(client)
        payload = _world_update(
            {
                "op": "upsert_entity",
                "data": {
                    "external_key": "entity:rolled-back",
                    "kind": "anomaly",
                    "name": "不应存在",
                },
            },
            {
                "op": "upsert_fact",
                "data": {
                    "kind": "rule",
                    "predicate": "missing-key",
                    "value": True,
                },
            },
        )
        created = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=payload,
        )
        candidate_id = created.json["data"]["candidate"]["id"]
        response = client.post(
            f"/api/runtime/narrative/candidates/{candidate_id}/accept",
            headers=headers,
        )
        assert response.status_code == 400

        with app.app_context():
            from models.narrative import (
                RuntimeNarrativeCandidate,
                RuntimeNarrativeRevision,
                RuntimeWorldEntity,
            )

            assert RuntimeWorldEntity.query.count() == 0
            assert RuntimeNarrativeRevision.query.count() == 0
            assert (
                RuntimeNarrativeCandidate.query.session.get(RuntimeNarrativeCandidate, candidate_id).status
                == "pending"
            )

    def test_revision_conflict_rolls_back_candidate_atomically(
        self,
        monkeypatch,
    ):
        app = _smoke_app(monkeypatch)
        client = app.test_client()
        headers = _auth(client)
        first = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=_world_update(
                {
                    "op": "upsert_entity",
                    "data": {
                        "external_key": "entity:versioned",
                        "kind": "personnel",
                        "name": "版本一",
                    },
                }
            ),
        ).json["data"]["candidate"]
        assert client.post(
            f"/api/runtime/narrative/candidates/{first['id']}/accept",
            headers=headers,
        ).status_code == 200

        update_payload = _world_update(
            {
                "op": "upsert_fact",
                "data": {
                    "fact_key": "fact:must-rollback",
                    "kind": "observation",
                    "predicate": "exists",
                    "value": True,
                },
            },
            {
                "op": "upsert_entity",
                "data": {
                    "external_key": "entity:versioned",
                    "kind": "personnel",
                    "name": "错误覆盖",
                    "expected_revision": 99,
                },
            },
        )
        update_payload["idempotency_key"] = "candidate-version-conflict"
        second = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=update_payload,
        ).json["data"]["candidate"]
        conflict = client.post(
            f"/api/runtime/narrative/candidates/{second['id']}/accept",
            headers=headers,
        )
        assert conflict.status_code == 409

        with app.app_context():
            from models.narrative import (
                RuntimeNarrativeCandidate,
                RuntimeNarrativeRevision,
                RuntimeWorldEntity,
                RuntimeWorldFact,
            )

            entity = RuntimeWorldEntity.query.one()
            assert entity.name == "版本一"
            assert entity.revision == 1
            assert RuntimeWorldFact.query.count() == 0
            assert RuntimeNarrativeRevision.query.count() == 1
            assert (
                RuntimeNarrativeCandidate.query.session.get(RuntimeNarrativeCandidate, second["id"]).status
                == "pending"
            )

    def test_visibility_scope_and_frozen_snapshot_are_deterministic(
        self,
        monkeypatch,
    ):
        client = _smoke_app(monkeypatch).test_client()
        headers = _auth(client)
        payload = _world_update(
            {
                "op": "upsert_entity",
                "data": {
                    "external_key": "entity:public",
                    "kind": "department",
                    "name": "公共前厅",
                    "visibility": "public",
                },
            },
            {
                "op": "upsert_entity",
                "data": {
                    "external_key": "entity:scoped",
                    "kind": "department",
                    "name": "A 区",
                    "visibility": "members",
                    "scope_keys": ["site-a"],
                },
            },
            {
                "op": "upsert_entity",
                "data": {
                    "external_key": "entity:secret",
                    "kind": "anomaly",
                    "name": "隐秘实体",
                    "visibility": "secret",
                },
            },
            {
                "op": "upsert_story_thread",
                "data": {
                    "external_key": "thread:site-a",
                    "kind": "investigation",
                    "title": "A 区失联",
                    "summary": "等待下一次揭示",
                    "visibility": "members",
                    "scope_keys": ["site-a"],
                },
            },
        )
        candidate = client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=payload,
        ).json["data"]["candidate"]
        assert (
            client.post(
                f"/api/runtime/narrative/candidates/{candidate['id']}/accept",
                headers=headers,
            ).status_code
            == 200
        )

        context = {
            "run_key": "expedition:7",
            "scope_keys": ["site-a"],
            "allowed_visibility": ["public", "members"],
        }
        preview = client.post(
            "/api/runtime/narrative/preview",
            headers=headers,
            json=context,
        )
        keys = {
            row["external_key"]
            for row in preview.json["data"]["bundle"]["entities"]
        }
        assert keys == {"entity:public", "entity:scoped"}

        frozen = client.post(
            "/api/runtime/narrative/snapshots",
            headers=headers,
            json=context,
        )
        assert frozen.status_code == 201
        frozen_again = client.post(
            "/api/runtime/narrative/snapshots",
            headers=headers,
            json=context,
        )
        assert frozen_again.status_code == 200
        assert frozen_again.json["data"]["created"] is False
        with client.application.app_context():
            from models.narrative import RuntimeStoryThread

            thread = RuntimeStoryThread.query.one()
            assert thread.injection_count == 1
            assert thread.last_injected_at is not None

        changed = {
            **context,
            "allowed_visibility": ["public"],
        }
        conflict = client.post(
            "/api/runtime/narrative/snapshots",
            headers=headers,
            json=changed,
        )
        assert conflict.status_code == 409

    def test_lease_requires_matching_owner_and_can_be_reacquired(
        self,
        monkeypatch,
    ):
        client = _smoke_app(monkeypatch).test_client()
        headers = _auth(client)
        first = client.post(
            "/api/runtime/narrative/leases/acquire",
            headers=headers,
            json={"lease_key": "outline:site-a", "ttl_seconds": 30},
        )
        assert first.status_code == 201
        token = first.json["data"]["owner_token"]
        assert (
            client.post(
                "/api/runtime/narrative/leases/acquire",
                headers=headers,
                json={
                    "lease_key": "outline:site-a",
                    "ttl_seconds": 30,
                },
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/runtime/narrative/leases/release",
                headers=headers,
                json={
                    "lease_key": "outline:site-a",
                    "owner_token": "wrong",
                },
            ).status_code
            == 409
        )
        released = client.post(
            "/api/runtime/narrative/leases/release",
            headers=headers,
            json={
                "lease_key": "outline:site-a",
                "owner_token": token,
            },
        )
        assert released.status_code == 200
        assert (
            client.post(
                "/api/runtime/narrative/leases/acquire",
                headers=headers,
                json={
                    "lease_key": "outline:site-a",
                    "ttl_seconds": 30,
                },
            ).status_code
            == 201
        )

    def test_concurrent_lease_acquire_has_exactly_one_winner(
        self,
        monkeypatch,
        tmp_path,
    ):
        app = _smoke_app(
            monkeypatch,
            database_uri=(
                f"sqlite:///{(tmp_path / 'lease-race.db').as_posix()}"
            ),
        )
        headers = _auth(app.test_client())
        with app.app_context():
            from db_repository import db_manager
            from models.narrative import RuntimeNarrativeLease

            ok, error = db_manager.add(
                RuntimeNarrativeLease(lease_key="outline:race")
            )
            assert ok, error
            ok, error = db_manager.commit()
            assert ok, error

        barrier = Barrier(2)

        def acquire():
            client = app.test_client()
            barrier.wait(timeout=5)
            return client.post(
                "/api/runtime/narrative/leases/acquire",
                headers=headers,
                json={
                    "lease_key": "outline:race",
                    "ttl_seconds": 30,
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(acquire) for _ in range(2)]
            statuses = sorted(future.result() for future in futures)
        assert statuses == [201, 409]

    def test_concurrent_candidate_accept_applies_exactly_once(
        self,
        monkeypatch,
        tmp_path,
    ):
        app = _smoke_app(
            monkeypatch,
            database_uri=(
                f"sqlite:///{(tmp_path / 'candidate-race.db').as_posix()}"
            ),
        )
        setup_client = app.test_client()
        headers = _auth(setup_client)
        candidate = setup_client.post(
            "/api/runtime/narrative/candidates",
            headers=headers,
            json=_world_update(
                {
                    "op": "upsert_entity",
                    "data": {
                        "external_key": "entity:accept-once",
                        "kind": "anomaly",
                        "name": "只能应用一次",
                    },
                }
            ),
        ).json["data"]["candidate"]
        barrier = Barrier(2)

        def accept():
            client = app.test_client()
            barrier.wait(timeout=5)
            response = client.post(
                (
                    "/api/runtime/narrative/candidates/"
                    f"{candidate['id']}/accept"
                ),
                headers=headers,
            )
            return (
                response.status_code,
                response.json["data"]["applied"],
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(accept) for _ in range(2)]
            results = sorted(future.result() for future in futures)
        assert results == [(200, False), (200, True)]
        with app.app_context():
            from models.narrative import (
                RuntimeNarrativeRevision,
                RuntimeWorldEntity,
            )

            assert RuntimeWorldEntity.query.count() == 1
            assert RuntimeNarrativeRevision.query.count() == 1
