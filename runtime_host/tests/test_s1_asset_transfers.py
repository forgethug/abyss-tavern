"""The exported pack executes exact transfers without a runtime balance patch."""
import pytest


@pytest.mark.parametrize('kind', ['skill', 'trait'])
def test_portable_exact_transfer_survives_reload_and_admin_undo(runtime_app, kind):
    from extensions import db
    from plugins.adventurer_s1.models import (
        AdventurerS1, AdventurerS1Skill, AdventurerS1Trait,
        AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog,
    )
    from plugins.adventurer_s1.services.s1_asset_transfer_service import apply_asset_transfers
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    with runtime_app.app_context():
        assert 'balance_limits_enabled' not in S1SettingsService.get_expedition_config()
        source = AdventurerS1(name='转出者', status='resting')
        target = AdventurerS1(name='接收者', status='resting')
        db.session.add_all([source, target])
        db.session.flush()
        model = AdventurerS1Skill if kind == 'skill' else AdventurerS1Trait
        asset = model(adventurer_id=source.id, name='原实例', description='原说明')
        db.session.add(asset)
        db.session.commit()
        source_id, target_id, asset_id = source.id, target.id, asset.id
        receipt = apply_asset_transfers([dict(asset_kind=kind, asset_id=asset_id,
            from_adventurer_id=source_id, to_adventurer_id=target_id, name=asset.name, event_indexes=[0])],
            allowed_member_ids=[source_id, target_id],
            frozen_assets={(source_id, kind, asset_id):{'name':asset.name,'description':asset.description}})[0]
        appeal = AdventurerS1DataAppeal(user_id=1, adventurer_id=target_id,
            player_note='已核实交接', selected_targets=[], snapshot_before={}, status='approved')
        db.session.add(appeal)
        db.session.flush()
        appeal_id = appeal.id
        db.session.add(AdventurerS1DataAppealAuditLog(appeal_id=appeal_id, adventurer_id=target_id,
            op_seq=1, op_type='transfer_asset', target_type=kind, target_id=asset_id,
            before_json=receipt['before'], after_json=receipt))
        db.session.commit()
        db.session.remove()
        asset = db.session.get(model, asset_id)
        assert asset.adventurer_id == target_id and asset.description == '原说明'
        result, error = AdventurerS1DataAppealService.revert_appeal(appeal_id, admin_id=1)
        assert error is None, error
        assert result['status'] == 'reverted'
        db.session.remove()
        restored = db.session.get(model, asset_id)
        assert restored.adventurer_id == source_id and restored.description == '原说明'
        assert db.session.query(model).filter_by(name='原实例').count() == 1


def test_portable_skill_transfer_keeps_live_dependency_ownership(runtime_app):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill, AdventurerS1Trait
    from plugins.adventurer_s1.services.s1_asset_transfer_service import apply_asset_transfers
    with runtime_app.app_context():
        source, target = AdventurerS1(name='来源'), AdventurerS1(name='目标')
        db.session.add_all([source, target])
        db.session.flush()
        skill = AdventurerS1Skill(adventurer_id=source.id, name='原技能', description='原说明')
        db.session.add(skill)
        db.session.flush()
        trait = AdventurerS1Trait(adventurer_id=source.id, name='依赖特质', description='关联原技能',
                                 asset_dependencies=[{'asset_kind':'skill','asset_id':skill.id}])
        db.session.add(trait)
        db.session.commit()
        row = dict(asset_kind='skill', asset_id=skill.id, from_adventurer_id=source.id,
                   to_adventurer_id=target.id, name=skill.name, event_indexes=[0])
        with pytest.raises(ValueError, match='依赖资产'):
            apply_asset_transfers([row], allowed_member_ids=[source.id, target.id])
        db.session.rollback()
        assert skill.adventurer_id == trait.adventurer_id == source.id
