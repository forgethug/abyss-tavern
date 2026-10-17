"""申诉操作组的在途携带资产保护；只读取持久化身份，不推断文本语义。"""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy.orm import selectinload

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1Equipment, AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember, AdventurerS1Skill, AdventurerS1Trait,
)

DEPARTURE_ASSET_IDS_KEY = '_departure_carried_asset_ids'
DEPARTURE_ASSET_LOCKED_CODE = 'departure_asset_locked'
_MODELS = {
    'equipment': AdventurerS1Equipment,
    'skills': AdventurerS1Skill,
    'traits': AdventurerS1Trait,
}


def freeze_departure_asset_ids(member_ids: list[int]) -> dict:
    """调用者持有全部成员锁；批量刷新关系，复用探险特质启用规则。"""
    from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
        trait_asset_dependency_states_for_members,
    )

    db_manager.flush()
    members = (
        db_manager.query(AdventurerS1)
        .filter(AdventurerS1.id.in_(member_ids))
        .options(
            selectinload(AdventurerS1.equipment_items),
            selectinload(AdventurerS1.skill_items),
            selectinload(AdventurerS1.traits),
        )
        .populate_existing().all()
    )
    active_trait_ids, _ = trait_asset_dependency_states_for_members(members)
    return {
        str(member.id): {
            'equipment': sorted(int(row.id) for row in member.equipment_items),
            'skills': sorted(int(row.id) for row in member.skill_items if row.equipped),
            'traits': sorted(int(row.id) for row in member.traits if row.id in active_trait_ids),
        }
        for member in members
    }


def load_departure_asset_ids(adventurer_id: int) -> dict[str, set[int]]:
    return load_departure_asset_ids_for_members([adventurer_id])[adventurer_id]


def load_departure_asset_ids_for_members(member_ids: list[int]) -> dict:
    """调用者已锁冒险者；不锁日志，避免引入与结算 log -> member 的反向锁序。"""
    logs = (
        db_manager.query(
            AdventurerS1ExpeditionLog.id,
            AdventurerS1ExpeditionLogMember.adventurer_id,
            AdventurerS1ExpeditionLog.full_log['audit_member_snapshot'].label('members'),
            AdventurerS1ExpeditionLog.full_log[DEPARTURE_ASSET_IDS_KEY].label('departure_ids'),
        )
        .join(AdventurerS1ExpeditionLogMember,
              AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id)
        .filter(
            AdventurerS1ExpeditionLogMember.adventurer_id.in_(member_ids),
            AdventurerS1ExpeditionLog.status.in_(('generating', 'in_progress')),
        ).all()
    )
    result = {mid: {section: set() for section in _MODELS} for mid in member_ids}
    for log in logs:
        adventurer_id = int(log.adventurer_id)
        protected = result[adventurer_id]
        member = next((
            row for row in (log.members or [])
            if isinstance(row, dict) and row.get('adventurer_id') == adventurer_id
        ), None)
        if member is not None:
            for section in protected:
                protected[section].update(
                    row['asset_id'] for row in member.get(section) or []
                    if isinstance(row, dict) and type(row.get('asset_id')) is int
                    and row['asset_id'] > 0
                )
        elif isinstance(log.departure_ids, dict) and str(adventurer_id) in log.departure_ids:
            for section in protected:
                protected[section].update(log.departure_ids[str(adventurer_id)].get(section) or [])
        else:
            # 旧版刚发车日志尚无身份快照：暂不能执行，不伪造携带身份或永久驳回整单。
            raise RuntimeError(f'探险 #{log.id} 的携带快照尚未就绪，请稍后重试申诉落库')
    return result


def capture_departure_asset_state(adventurer_id: int, protected: dict) -> dict:
    return capture_departure_asset_states({adventurer_id: protected})[adventurer_id]


def capture_departure_asset_states(protected_by_member: dict) -> dict:
    """按资产类型批量读取，比较真实写入副作用（含同名合并和改名联动）。"""
    state = {mid: {} for mid in protected_by_member}
    for section, model in _MODELS.items():
        ids = {rid for protected in protected_by_member.values()
               for rid in protected.get(section) or set()}
        if not ids:
            continue
        columns = [col for col in model.__table__.columns
                   if col.name not in ('created_at', 'updated_at')]
        rows = db_manager.query(*columns).filter(
            model.adventurer_id.in_(sorted(protected_by_member)), model.id.in_(sorted(ids)),
        ).order_by(model.id.asc()).with_for_update().all()
        for row in rows:
            values = dict(row._mapping)
            state[int(values['adventurer_id'])][(section, int(values['id']))] = deepcopy(values)
    return state


def departure_asset_change_error(before: dict, after: dict) -> dict | None:
    changed = sorted(key for key in before.keys() | after.keys()
                     if before.get(key) != after.get(key))
    if not changed:
        return None
    labels = {'equipment': '装备', 'skills': '技能', 'traits': '特质'}
    names = '、'.join(
        f'{labels[section]}「{(before.get((section, rid)) or after[(section, rid)]).get("name") or rid}」'
        for section, rid in changed
    )
    return {
        'code': DEPARTURE_ASSET_LOCKED_CODE,
        'detail': f'{names}已随本次探险出发，本项申诉不能在探险结束前修改携带内容；本项未执行，请在探险结束后在原申诉中继续处理。',
    }
