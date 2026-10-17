import importlib
import json
from types import SimpleNamespace


SOURCE_DESCRIPTION = "每次发动可凭空生成三块晶体，储存空间和已存物品跨探险持续保留。"
STORAGE_DESCRIPTION = "随时开启常驻储存空间，内容跨探险保留，没有额外原料或冷却要求。"


def test_old_server_exclusions_do_not_hide_current_assets(runtime_app):
    from plugins.adventurer_s1.services import s1_ai_input_policy_service as policy
    from plugins.adventurer_s1.services.s1_description_ai_service import description_source_hash

    source = SimpleNamespace(id=1, name="物质创造", description=SOURCE_DESCRIPTION, equipped=True)
    member = SimpleNamespace(id=7, traits=[], skill_items=[source], equipment_items=[])
    receipt = {
        "decision": "exclude", "reason_code": "economic_output_override",
        "schema_version": policy.AI_INPUT_POLICY_VERSION,
        "evidence_quote": "凭空生成三块晶体",
    }
    policies = {description_source_hash(SOURCE_DESCRIPTION, kind="skill"): receipt}
    with runtime_app.app_context():
        view = policy.project_members_for_ai([member], policies=policies)[0]
        saved = policy.project_member_snapshots_for_ai(
            [{"id": 7, "skills": [{"name": source.name, "description": source.description}]}],
            policies=policies,
        )
    assert view.skill_items == [source]
    assert saved[0]["skills"][0]["description"] == SOURCE_DESCRIPTION
    assert receipt["decision"] == "exclude"  # Historic audit evidence stays intact.
    assert member.skill_items == [source]


def test_generation_and_paired_reviews_share_self_hosted_rules(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_prompt_service as prompt
    from plugins.adventurer_s1.services import s1_ai_prompt_shared as shared

    modules = {
        "s1_building_ai_service": (
            "_BLACKSMITH_SYSTEM_PROMPT", "_TAILOR_WORKSHOP_SYSTEM_PROMPT",
            "_ALCHEMY_SYSTEM_PROMPT", "_ITEM_ASSEMBLY_SYSTEM_PROMPT",
            "_EMBER_KITCHEN_SYSTEM_PROMPT", "_PET_EVOLUTION_SYSTEM_PROMPT",
            "_SKILL_TRAINING_SYSTEM_PROMPT",
        ),
        "s1_asset_description_update_ai_service": (
            "_ASSET_DESCRIPTION_UPDATE_SYSTEM_PROMPT", "_ASSET_DESCRIPTION_UPDATE_REVIEW_SYSTEM_PROMPT",
        ),
    }
    with runtime_app.app_context():
        rendered = [prompt.build_s1_expedition_system_prompt()]
        for name, fields in modules.items():
            module = importlib.import_module("plugins.adventurer_s1.services." + name)
            rendered.extend(getattr(module, field) for field in fields)
    for text in rendered:
        assert "能力与产物可以跨探险持续存在" in text
        assert "不要求另有原料" in text
        assert "只有有界产物承诺时 reject" not in text
        assert "个人资产中的独立产物或额外收益承诺，在探险中不作为产出依据" not in text
        assert "不得直接设定、增减、倍乘" not in text
    assert "200 个冒险者" not in rendered[0]
    assert "力量/敏捷/智力/运气字段各不超过 200，生命上限字段不超过 1000" in rendered[0]
    assert "后端写入权限" in shared.S1_SKILL_RESOURCE_AUTHORITY_BOUNDARY


def test_portable_defaults_keep_the_five_selected_caps(runtime_app):
    assert runtime_app.config["ADVENTURER_S1_ATTR_CAP"] == 200
    assert runtime_app.config["ADVENTURER_S1_MAX_HP_CAP"] == 1000
    assert runtime_app.config["ADVENTURER_S1_EQUIPMENT_LIMIT"] == 5
    assert runtime_app.config["ADVENTURER_S1_MAX_EQUIPPED_SKILLS"] == 5
    assert runtime_app.config["ADVENTURER_S1_WAREHOUSE_CAPACITY"] == 100


def _settlement(member_id):
    return {
        "summary": "能力生成的物品及长期能力已实际取得并保留。",
        "events": [
            "物质创造发动后，三块晶体在空白桌面上形成。",
            "冒险者收好晶体，完成常驻储存空间的练习。",
            "他带着三块晶体和持续生效的储存空间返回酒馆。",
        ],
        "completion_check": {
            "story_finished": True, "core_resolved": True,
            "exit_resolved": True, "ending_type": "returned",
        },
        "member_updates": [{
            "id": member_id, "remaining_hp": 995, "is_dead": False,
            "strength_increase": 50, "max_hp_increase": 100,
            "new_skills": [{"name": "常驻空间", "desc": STORAGE_DESCRIPTION,
                            "recovery_minutes": 0, "administrative_override": True}],
            "new_equipments": {"创造晶体": {
                "type": "material", "rarity": "common", "quantity": 3,
                "desc": "由物质创造生成的晶体，可长期保留。",
                "grant_other_tavern": True,
            }},
            "raw_sql": "unused extra",
        }],
        "raw_system_command": "unused extra",
    }


def test_generated_resources_and_persistent_ability_survive_settlement_and_next_input(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill
    from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
        parse_s1_expedition_response, validate_s1_expedition_result,
    )
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        apply_update_to_s1_adventurer, _build_s1_expedition_member_snapshots,
    )
    from plugins.adventurer_s1.services.adventurer_s1_prompt_service import build_s1_member_block
    from plugins.adventurer_s1.services.s1_ai_input_policy_service import project_members_for_ai

    with runtime_app.app_context():
        adv = AdventurerS1(name="铸造者", strength=190, max_hp=995, hp=995)
        db.session.add(adv)
        db.session.flush()
        db.session.add_all([
            AdventurerS1Skill(adventurer_id=adv.id, name="物质创造",
                              description=SOURCE_DESCRIPTION, equipped=True),
            *[AdventurerS1Skill(adventurer_id=adv.id, name=f"既有技能{i}",
                                description="可以主动运用的既有本领。", equipped=True)
              for i in range(3)],
        ])
        db.session.commit()
        member_id = adv.id
        parsed = parse_s1_expedition_response(json.dumps(_settlement(member_id), ensure_ascii=False))
        assert parsed is not None
        valid, errors, _ = validate_s1_expedition_result(parsed, [member_id], skip_length_checks=True)
        assert valid, errors
        serialized = json.dumps(parsed, ensure_ascii=False)
        for extra in ("raw_system_command", "raw_sql", "administrative_override", "grant_other_tavern"):
            assert extra not in serialized
        apply_update_to_s1_adventurer(adv, parsed["member_updates"][0], {}, record_history=False)
        db.session.commit()
        db.session.remove()

        restored = db.session.get(AdventurerS1, member_id)
        assert restored.strength == 200
        assert restored.max_hp == 1000
        assert len([s for s in restored.skill_items if s.equipped]) == 5
        persistent = next(s for s in restored.skill_items if s.name == "常驻空间")
        assert persistent.description == STORAGE_DESCRIPTION
        assert persistent.recovery_minutes == 0
        generated = next(e for e in restored.equipment_items if e.name == "创造晶体")
        assert generated.quantity == 3
        snapshots, _ = _build_s1_expedition_member_snapshots([restored])
        next_prompt = json.loads(build_s1_member_block(project_members_for_ai(snapshots)[0], {}))
        assert next(row for row in next_prompt["skills"] if row["name"] == "常驻空间")["description"] == STORAGE_DESCRIPTION
        assert next(row for row in next_prompt["equipment"] if row["name"] == "创造晶体")["quantity"] == 3


def test_new_persistent_skill_is_saved_unequipped_when_five_slots_are_full(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import apply_update_to_s1_adventurer

    with runtime_app.app_context():
        adv = AdventurerS1(name="满栏者", strength=20, max_hp=100, hp=100)
        db.session.add(adv)
        db.session.flush()
        db.session.add_all([
            AdventurerS1Skill(adventurer_id=adv.id, name=f"既有技能{i}",
                              description="可以主动运用的既有本领。", equipped=True)
            for i in range(5)
        ])
        db.session.commit()
        member_id = adv.id
        update = {"new_skills": [{"name": "常驻空间", "desc": STORAGE_DESCRIPTION,
                                  "recovery_minutes": 0}]}
        apply_update_to_s1_adventurer(adv, update, {}, record_history=False)
        db.session.commit()
        db.session.remove()
        restored = db.session.get(AdventurerS1, member_id)
        assert len(restored.skill_items) == 6
        assert sum(bool(skill.equipped) for skill in restored.skill_items) == 5
        persistent = next(skill for skill in restored.skill_items if skill.name == "常驻空间")
        assert not persistent.equipped
        assert persistent.description == STORAGE_DESCRIPTION


def test_self_hosted_rules_keep_member_identity_validation(runtime_app):
    from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
        parse_s1_expedition_response, validate_s1_expedition_result,
    )

    with runtime_app.app_context():
        parsed = parse_s1_expedition_response(json.dumps(_settlement(999)))
        valid, errors, _ = validate_s1_expedition_result(parsed, [7], skip_length_checks=True)
    assert not valid
    assert errors


def test_fixed_server_database_relays_are_absent_and_admin_guards_remain(runtime_client):
    assert runtime_client.get("/api/adventurer-s1/admin/database-read/tables").status_code == 404
    assert runtime_client.get("/api/adventurer-s1/admin/adventurers").status_code in {401, 403}
