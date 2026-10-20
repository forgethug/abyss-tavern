import copy

import pytest

from conftest import S1_ADAPTER_PATH
from runtime_core.pack_adapter import load_runtime_extension_factory
from test_s1_platform_boundaries import _admin_headers
from test_tavern_model_config import make_app


def seed_log(app, *, name="测试冒险者", status="completed"):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1ExpeditionLog

    with app.app_context():
        member = AdventurerS1(name=name)
        db.session.add(member)
        db.session.flush()
        full_log = {
            "events": ["已发生事件", "尚未公开事件"],
            "current_event_index": 1,
            "summary": "测试结算",
            "member_updates": [
                {"id": member.id, "hp_change": -2},
                {"id": str(member.id), "name": "日志中保留的名字", "gold_change": 5},
                {"id": 999999, "hp_change": 0},
            ],
            "admin_evidence": {"reason": "仅管理员可见"},
        }
        log = AdventurerS1ExpeditionLog(
            target_dungeon="测试矿坑", status=status, full_log=copy.deepcopy(full_log),
        )
        db.session.add(log)
        db.session.commit()
        return log.id, member.id, full_log


@pytest.mark.parametrize("status", ["completed", "failed", "in_progress", "generating"])
def test_admin_detail_names_preserve_full_log_and_stored_evidence(runtime_app, status):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog

    client = runtime_app.test_client()
    headers = _admin_headers(runtime_app, client)
    log_id, member_id, original = seed_log(runtime_app, status=status)
    response = client.get(f"/api/adventurer-s1/logs/{log_id}?reveal_full=1", headers=headers)
    assert response.status_code == 200, response.json
    full_log = response.json["data"]["full_log"]
    expected = copy.deepcopy(original)
    expected["member_updates"][0]["name"] = "测试冒险者"
    assert full_log == expected
    assert full_log["member_updates"][0]["id"] == member_id
    with runtime_app.app_context():
        db.session.expire_all()
        assert db.session.get(AdventurerS1ExpeditionLog, log_id).full_log == original
        assert not db.session.dirty


def test_player_cannot_reveal_admin_evidence_and_inflight_stays_hidden(runtime_app):
    client = runtime_app.test_client()
    headers = _admin_headers(runtime_app, client)
    log_id, _, _ = seed_log(runtime_app, status="in_progress")
    # Even admins retain the filtered in-flight view unless they request full detail.
    response = client.get(f"/api/adventurer-s1/logs/{log_id}", headers=headers)
    assert response.json["data"]["full_log"]["member_updates"] == []
    assert response.json["data"]["full_log"]["events"] == ["已发生事件"]
    client.post(
        "/api/runtime/identity/sync",
        headers={"X-Runtime-Bootstrap-Secret": "test-bootstrap-secret-private-32-bytes"},
        json={"id": 71, "username": "portable-admin", "is_admin": False, "is_active": True},
    )
    for status in ("in_progress", "completed"):
        log_id, _, _ = seed_log(runtime_app, status=status)
        response = client.get(f"/api/adventurer-s1/logs/{log_id}?reveal_full=1", headers=headers)
        assert response.status_code == 200, response.json
        full_log = response.json["data"]["full_log"]
        assert "admin_evidence" not in full_log
        if status == "in_progress":
            assert full_log["member_updates"] == []
        else:
            assert full_log["member_updates"][0]["name"] == "测试冒险者"
    assert client.get(f"/api/adventurer-s1/logs/{log_id}").status_code == 401


def test_names_are_batched_and_resolved_by_id_not_row_order(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import adventurer_s1_log_service

    calls = []

    def names(ids):
        calls.append(ids)
        return {2: "第二人", 1: "第一人"}

    monkeypatch.setattr(adventurer_s1_log_service, "_fetch_adventurer_names", names)
    enrich = load_runtime_extension_factory(S1_ADAPTER_PATH, "log_presentation.py", "with_member_names")
    full_log = {"member_updates": [
        {"id": "2"}, {"id": 1}, {"id": 2}, {"id": 3, "name": "历史名字"},
        {"id": 999}, {"id": True}, {"id": 1.5}, {"id": "bad"}, None,
    ]}
    before = copy.deepcopy(full_log)
    with runtime_app.app_context():
        result = enrich(full_log)
    assert calls == [[1, 2, 999]]
    assert [entry["name"] for entry in result["member_updates"][:4]] == [
        "第二人", "第一人", "第二人", "历史名字",
    ]
    assert result["member_updates"][4:] == full_log["member_updates"][4:]
    assert full_log == before
    calls.clear()
    assert enrich({"member_updates": [{"id": 1, "name": "已有名字"}]}) == {
        "member_updates": [{"id": 1, "name": "已有名字"}],
    }
    assert calls == []


def test_same_ids_in_two_taverns_do_not_share_names(tmp_path):
    apps = [make_app(tmp_path / f"{name}.db", name) for name in ("a", "b")]
    fixtures = []
    for app, name in zip(apps, ("甲酒馆冒险者", "乙酒馆冒险者")):
        client = app.test_client()
        headers = _admin_headers(app, client)
        log_id, member_id, _ = seed_log(app, name=name)
        fixtures.append((client, headers, log_id, member_id, name))
    assert fixtures[0][2:4] == fixtures[1][2:4]
    for client, headers, log_id, _, name in [*fixtures, fixtures[0]]:
        response = client.get(f"/api/adventurer-s1/logs/{log_id}?reveal_full=1", headers=headers)
        assert response.json["data"]["full_log"]["member_updates"][0]["name"] == name


def test_missing_log_keeps_original_error(runtime_app):
    client = runtime_app.test_client()
    headers = _admin_headers(runtime_app, client)
    response = client.get("/api/adventurer-s1/logs/999999?reveal_full=1", headers=headers)
    assert response.status_code == 404
