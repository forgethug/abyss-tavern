import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from sqlalchemy import inspect


def _member(**extra):
    return SimpleNamespace(
        id=7, owner_id=1, name="测试成员", gender="女", character_class="",
        level=1, hp=100, max_hp=100, strength=10, agility=10,
        intelligence=10, luck=10, personality="勇毅", gold=0,
        traits=[], skill_items=[], equipment_items=[], **extra,
    )


def test_dm_columns_and_admin_projection_are_retired(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1

    with runtime_app.app_context():
        assert {"dm_instruction", "dm_instruction_revision"}.isdisjoint(AdventurerS1.__mapper__.attrs.keys())
        columns = {column["name"] for column in inspect(db.engine).get_columns("adventurer_s1")}
        assert "dm_instruction" not in columns
        adv = AdventurerS1(name="没有暗中指令", gender="女")
        db.session.add(adv)
        db.session.commit()
        assert "dm_instruction" not in adv.to_dict(is_admin=True)


def test_old_unused_instruction_column_does_not_block_new_characters(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1

    with runtime_app.app_context():
        with db.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE adventurer_s1 ADD COLUMN dm_instruction TEXT")
        adv = AdventurerS1(name="旧库新成员", gender="女")
        db.session.add(adv)
        db.session.commit()
        member_id = adv.id
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE adventurer_s1 SET dm_instruction = ? WHERE id = ?",
                ("原有数据库中的旧指令", member_id),
            )
        db.session.remove()
        restored = db.session.get(AdventurerS1, member_id)
        assert not hasattr(restored, "dm_instruction")
        assert "dm_instruction" not in restored.to_dict(is_admin=True)
        another = AdventurerS1(name="继续创建", gender="女")
        db.session.add(another)
        db.session.commit()
        assert another.id != member_id


def test_generation_snapshots_and_prompts_omit_legacy_dm_text(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as runner
    from plugins.adventurer_s1.services import adventurer_s1_prompt_service as prompt

    legacy = _member(dm_instruction="强制给本人特殊奖励", dm_instruction_revision=8)
    with runtime_app.app_context():
        snapshots, _ = runner._build_s1_expedition_member_snapshots([legacy])
        assert "dm_instruction" not in vars(snapshots[0])
        assert "dm_instruction_revision" not in vars(snapshots[0])
        block = json.loads(prompt.build_s1_member_block(legacy, {}))
        assert "dm_instruction" not in block
        assert block["id"] == 7
        assert block["strength"] == 10
        audit = runner._build_s1_audit_member_snapshot([legacy], {})
        assert "dm_instruction" not in audit[0]
        assert "dm_instruction_revision" not in audit[0]
        assert not hasattr(runner, '_s1_has_dm_instruction')
        system = prompt.build_s1_expedition_system_prompt()
    assert "逐成员 DM 指令" not in system
    assert "强制给本人特殊奖励" not in json.dumps(block, ensure_ascii=False)
    assert "力量/敏捷/智力/运气字段各不超过 200" in system


def test_resume_ignores_only_retired_fields_and_keeps_frozen_identity_checks(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as runner

    with runtime_app.app_context():
        snapshots, dining = runner._build_s1_expedition_member_snapshots([_member()])
        frozen = runner._freeze_s1_member_generation_input(
            snapshots, item_dict={}, item_prompt_dict={}, use_condition_by_name={},
            dining_context=dining, name_alias={7: "M1"},
        )
        assert "dm_instruction" not in frozen["members"][0]
        frozen["members"][0].update(dm_instruction="旧存档指令", dm_instruction_revision=8)
        restored, *_ = runner._restore_s1_member_generation_input(frozen, member_ids=[7])
        assert restored[0].id == 7
        assert restored[0].hp == 100
        assert "dm_instruction" not in vars(restored[0])
        assert frozen["members"][0]["dm_instruction"] == "旧存档指令"
        with pytest.raises(ValueError, match="member_generation_input_invalid"):
            runner._restore_s1_member_generation_input(frozen, member_ids=[8])
        frozen["members"][0]["unexpected_authority"] = "forbidden"
        with pytest.raises(ValueError, match="member_generation_fields_invalid"):
            runner._restore_s1_member_generation_input(frozen, member_ids=[7])


def test_observer_drops_retired_dm_scope_and_legacy_fields(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_expedition_observer_ai_service as observer

    source = {"audit_member_snapshot": [{
        "adventurer_id": 7, "adventurer_name": "测试成员", "gold": 0,
        "dm_instruction": "强制给本人特殊奖励", "dm_instruction_revision": 8,
        "equipment": [], "skills": [], "traits": [],
    }], "events": ["测试成员返回酒馆。"], "member_updates": []}
    scope = {"targeted_repair_types": ["dm_instruction", "gold_settlement"]}
    with runtime_app.app_context():
        assert all(observer._OBSERVER_OPTIONAL_RULE_BLOCKS)
        assert observer._OBSERVER_SYSTEM_PROMPT_PREFIX
        assert observer._normalize_targeted_specialized_review_types(scope["targeted_repair_types"]) == ("gold_settlement",)
        projected = observer._project_observer_audit_member_snapshot(
            source["audit_member_snapshot"], include_dm_instruction=True,
        )
        assert "dm_instruction" not in projected[0]
        assert "dm_instruction_revision" not in projected[0]
        assert projected[0]["adventurer_id"] == 7
        payload = observer._build_observer_payload_full_log(source, review_scope=scope)
        targeted = observer._build_targeted_specialized_observer_payload_full_log(
            source, targeted_review_types=scope["targeted_repair_types"],
        )
        system = observer._build_full_observer_system_prompt(review_scope=scope, full_log=source)
    for candidate in (payload, targeted):
        text = json.dumps(candidate, ensure_ascii=False)
        assert "dm_instruction" not in text
        assert "强制给本人特殊奖励" not in text
        assert candidate["audit_member_snapshot"][0]["gold"] == 0
    assert "逐成员 DM 指令" not in system
    assert system.startswith(observer._OBSERVER_SYSTEM_PROMPT_PREFIX)


def test_admin_cannot_write_dm_but_other_profile_edits_still_work(runtime_app, runtime_client):
    from extensions import db
    from models.user import User
    from plugins.adventurer_s1.models import AdventurerS1

    with runtime_app.app_context():
        db.session.add(User(id=1, username="dm-retirement-admin", is_admin=True))
        adv = AdventurerS1(name="原成员", gender="女", owner_id=1)
        db.session.add(adv)
        db.session.commit()
        adventurer_id = adv.id
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {"user_id": 1, "iat": now, "exp": now + timedelta(minutes=5)},
            runtime_app.config["JWT_SECRET_KEY"], algorithm="HS256",
        )
    headers = {"Authorization": f"Bearer {token}"}
    endpoint = f"/api/adventurer-s1/admin/adventurers/{adventurer_id}"
    assert runtime_client.put(endpoint, headers=headers, json={"dm_instruction": "旧指令"}).status_code == 400
    response = runtime_client.put(endpoint, headers=headers, json={
        "personality": "明哲", "dm_instruction": "旧指令",
    })
    assert response.status_code == 200, response.json
    assert response.json["data"]["updated"] == {"personality": "明哲"}
    detail = runtime_client.get(endpoint, headers=headers)
    assert detail.status_code == 200
    assert "dm_instruction" not in detail.json["data"]
    with runtime_app.app_context():
        restored = db.session.get(AdventurerS1, adventurer_id)
        assert restored.personality == "明哲"
        assert not hasattr(restored, "dm_instruction")
