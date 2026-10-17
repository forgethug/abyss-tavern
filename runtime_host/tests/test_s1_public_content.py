"""Public defaults must not inherit personal identities or payment destinations."""
import ast
import runpy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / 'game-packs/s1-fantasy'


def test_titles_have_no_account_specific_eligibility(runtime_app):
    from plugins.adventurer_s1.services.s1_title_service import (
        S1TitleService, seed_s1_titles, VALID_FRAME_KEYS,
    )
    with runtime_app.app_context():
        seed_s1_titles()
        assert set(VALID_FRAME_KEYS) == {'standard', 'fate', 'fate_companion', 'hidden_relic'}
        for uid in (3, 652, 999):
            adv = SimpleNamespace(id=10000 + uid, owner_id=uid)
            assert S1TitleService._automatic_codes(adv) == set()
            assert not [r for r in S1TitleService._player_title_rows(adv)
                        if S1TitleService._row_is_available(adv, r)]
            # Manual grants and earned titles remain available for every user.
            title = SimpleNamespace(code='custom_title')
            assert S1TitleService._row_is_available(adv, (title, True, False, 0))
            fate = SimpleNamespace(code='fate_engraver')
            assert S1TitleService._row_is_available(adv, (fate, False, True, 0))


def test_dorms_use_one_pool_and_preserve_occupancy_checks(runtime_app):
    from extensions import db
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1DormRoom
    from plugins.adventurer_s1.seed import seed_dorm_rooms, BUILDING_TEMPLATES
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Service
    with runtime_app.app_context():
        assert seed_dorm_rooms() > 0
        assert seed_dorm_rooms() == 0
        assert not AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.room_key.isnot(None)).all()
        assert any(row[0] == 'tavern_owner_statue' for row in BUILDING_TEMPLATES)
        room = AdventurerS1DormRoom.query.filter_by(capacity=1).first()
        first = AdventurerS1(name='first', gender='女', owner_id=3)
        second = AdventurerS1(name='second', gender='男', owner_id=652)
        db.session.add_all([first, second]); db.session.commit()
        with db_manager.transaction():
            assert Service._claim_bed_slot(first, room.id, 0)
            assert not Service._claim_bed_slot(second, room.id, 0)
            assert not Service._claim_bed_slot(second, room.id, 1)
        assert Service._room_allows_adventurer(room, first)
        assert Service._room_allows_adventurer(room, second)
        assert (room.id, 0) not in Service._collect_free_bed_slots()
        groups = [[SimpleNamespace(user_id=652), SimpleNamespace(user_id=3)], [SimpleNamespace(user_id=999)]]
        assert Service._select_draw_reservations(groups, count=2, ordinary_bed_count=0) == []
        assert [r.user_id for r in Service._select_draw_reservations(groups, count=2, ordinary_bed_count=1)] == [652]
        assert Service.get_draw_capacity_stats()['alive_count'] == 2


def test_legacy_migration_preserves_residents_and_history(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1DormRoom, AdventurerS1Title, AdventurerS1TitleGrant
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Service
    migrate = runpy.run_path(str(PACK / 'runtime_adapter/public_content_migration.py'))['migrate']
    with runtime_app.app_context():
        room = AdventurerS1DormRoom(name='酒馆老板的房间', room_key='tavern_owner_room', capacity=1, level=1)
        old = AdventurerS1Title(code='pillow_developer', name='old', description='history', frame_key='developer', is_system=True, is_enabled=True)
        custom = AdventurerS1Title(code='custom', name='custom', frame_key='standard', is_system=False, is_enabled=True)
        db.session.add_all([room, old, custom]); db.session.flush()
        adv = AdventurerS1(name='resident', gender='女', owner_id=3, dorm_room_id=room.id, bed_index=0, active_title_id=old.id)
        db.session.add(adv); db.session.flush()
        grant = AdventurerS1TitleGrant(adventurer_id=adv.id, title_id=old.id, granted_by_user_id=3)
        db.session.add(grant); db.session.commit()
        ids = room.id, adv.id, old.id, grant.id
        migrate(); migrate()
        assert (room.id, adv.id, old.id, grant.id) == ids
        assert room.room_key is None and room.capacity == 1
        assert room.name == f'单人间 {room.id}'
        assert (adv.dorm_room_id, adv.bed_index) == (room.id, 0)
        assert Service._has_valid_bed(adv)
        assert adv.get_recovery_rate() == 1
        assert not old.is_enabled and old.description == 'history'
        assert custom.is_enabled
        assert adv.to_dict()['active_title'] is None
        assert db.session.get(AdventurerS1TitleGrant, grant.id) is not None


def test_public_sources_remove_personal_content_and_keep_learning():
    ui = ROOT / 'frontend-v2/src/plugins/adventurer_s1'
    for path in ui.rglob('*'):
        if path.suffix not in {'.vue', '.ts', '.scss'}: continue
        text = path.read_text(encoding='utf-8')
        for marker in ('kaoyu', 'pillow_developer', 'dev-core', 'dev-corner', 'SponsorTipModal', 'sponsorImageUrl', 'showSponsorTip', 'UID 3', 'is_tavern_owner_room'):
            assert marker not in text, (path, marker)
    assert not (ROOT / 'frontend-v2/src/assets/adventurer-s1-sponsor.jpg').exists()
    assert not (PACK / 'source-snapshot/frontend-v2/src/assets/adventurer-s1-sponsor.jpg').exists()
    assert not (PACK / 'runtime_adapter/retired_dm_instructions.py').exists()
    for filename in ('adventurer_s1_prompt_service.py', 'adventurer_s1_expedition_observer_ai_service.py'):
        path = PACK / 'source-snapshot/backend/plugins/adventurer_s1/services' / filename
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert '逐成员 DM 指令' not in node.value
                assert 'DM 暗中提示' not in node.value
                assert '[DM暗中提示]' not in node.value
                assert 'DM 对某名成员的幸存约束' not in node.value
    import hashlib, json
    manifest = json.loads((PACK / 'source-snapshot/PROJECTION.json').read_text(encoding='utf-8'))
    learning_files = [relative for relative in manifest if '/s1_countermeasure_' in relative or '/s1_encounter_preparation_' in relative]
    assert len(learning_files) >= 10
    for relative in learning_files:
        digest = manifest[relative]
        assert digest['unchanged_except_python_compat'] is True
        assert hashlib.sha256((PACK / 'source-snapshot' / relative).read_bytes()).hexdigest() == digest['portable_sha256']


def test_projection_is_source_only_and_rejects_missing_editor_anchor():
    projection = runpy.run_path(str(ROOT / 'tools/s1_public_source.py'))
    import pytest
    with pytest.raises(ValueError, match='source drift'):
        projection['project_public_source']('frontend-v2/src/plugins/adventurer_s1/components/AdminAdventurerPanel.vue', b'<template />')
    source = b'def untouched():\n    return "user text"\n'
    assert projection['project_public_source']('unrelated.py', source) == source


def test_personal_cross_realm_entry_points_are_closed(runtime_app):
    import pytest
    from plugins.adventurer_s1.services import s1_cross_realm_mirror_service as mirror
    with runtime_app.app_context():
        for uid in (3, 652, 999):
            assert not mirror.is_cross_realm_mirror_control_visible(uid)
            with pytest.raises(mirror.S1CrossRealmMirrorStateError, match='不启用'):
                mirror.arm_cross_realm_mirror(uid)
        with pytest.raises(mirror.S1CrossRealmMirrorStateError, match='不启用'):
            mirror.offer_cross_realm_mirror_from_region_2(
                runtime_id='isolated-runtime', deployment_id='test',
                expedition_log_id=1, target_dungeon='test',
            )
    helper = PACK / 'source-snapshot/backend/plugins/adventurer/services/adventurer_prompt_service.py'
    assert 'dm_instruction' not in helper.read_text(encoding='utf-8')
