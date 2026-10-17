"""S1 探险成员结算到账回执。

只核对结构化结算字段与事务内真实持久化状态，不做自然语言语义判断。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Currency,
    AdventurerS1Equipment,
    AdventurerS1Skill,
    AdventurerS1Trait,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
    normalized_member_new_trait_names,
    parse_member_removed_trait_names,
    resolve_removed_trait_db_name,
)


class S1SettlementReceiptMismatch(RuntimeError):
    """成员结算写入结果与结构化承诺不一致。"""

    def __init__(self, receipt: dict):
        self.receipt = receipt
        mismatches = receipt.get('mismatches') or []
        super().__init__(f'S1 结算到账回执不一致: {mismatches[:20]}')


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0')
    if not amount.is_finite():
        amount = Decimal('0')
    return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _positive_int(value, default: int = 1) -> int:
    try:
        return max(1, min(int(value), 999999))
    except (TypeError, ValueError):
        return default


def _item_units(quantity, uses_left) -> int:
    quantity_value = _positive_int(quantity)
    if quantity_value > 1:
        return quantity_value
    if uses_left is not None:
        return _positive_int(uses_left)
    return 1


def _named_entries(payload) -> dict[str, int]:
    """把已规范化的新增对象压成 name -> quantity。"""
    result: dict[str, int] = defaultdict(int)
    from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
        normalize_s1_new_equipment_payload,
    )
    from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix

    entries = (normalize_s1_new_equipment_payload(payload) or {}).items()
    for raw_name, raw_data in entries:
        data = raw_data if isinstance(raw_data, dict) else {}
        name, quantity = split_stack_suffix(raw_name, data.get('quantity', 1))
        name = str(name or '').strip()
        if not name:
            continue
        result[name] += _item_units(quantity, data.get('uses_left'))
    return dict(result)


def _named_asset_quantities(payload) -> dict[str, int]:
    """把新增对象压成 name -> 实物件数，不把单件剩余次数当件数。"""
    result: dict[str, int] = defaultdict(int)
    from plugins.adventurer_s1.services.adventurer_s1_parser_service import (
        normalize_s1_new_equipment_payload,
    )
    from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix

    entries = (normalize_s1_new_equipment_payload(payload) or {}).items()
    for raw_name, raw_data in entries:
        data = raw_data if isinstance(raw_data, dict) else {}
        name, quantity = split_stack_suffix(raw_name, data.get('quantity', 1))
        name = str(name or '').strip()
        if name:
            result[name] += _positive_int(quantity)
    return dict(result)


def _removed_entries(payload) -> dict[str, int]:
    """把已规范化的消耗对象压成 name -> quantity。"""
    result: dict[str, int] = defaultdict(int)
    entries = payload if isinstance(payload, list) else [payload]
    for item in entries[:100]:
        if isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            quantity = _positive_int(item.get('quantity', 1))
        else:
            name = str(item or '').strip()
            quantity = 1
        if name:
            result[name] += quantity
    return dict(result)


def _names(payload) -> set[str]:
    if isinstance(payload, dict):
        return {str(name).strip() for name in payload if str(name).strip()}
    entries = payload if isinstance(payload, list) else [payload]
    result = set()
    for item in entries[:100]:
        name = item.get('name') if isinstance(item, dict) else item
        clean = str(name or '').strip()
        if clean:
            result.add(clean)
    return result


def _normalized_skill_names(payload) -> set[str]:
    """按技能写入器的机械规范化口径读取名称，不做语义拆分。"""
    from plugins.adventurer.services.adventurer_parser_service import (
        _normalize_new_skills_payload,
    )

    return {
        str(skill.get('name') or '').strip()
        for skill in _normalize_new_skills_payload(
            payload,
            split_semantic_name_desc=False,
        )
        if isinstance(skill, dict) and str(skill.get('name') or '').strip()
    }


def _iso_timestamp(value) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if callable(isoformat) else str(value)


def _append_asset_mutation(
    mutations: list[dict],
    *,
    asset_type: str,
    asset_id: int,
    before_row: dict | None,
    after_row: dict | None,
) -> None:
    """记录权威表真实发生的变化；相同前后值不制造伪流水。"""
    if after_row is None or before_row == after_row:
        return
    mutations.append({
        'asset_type': asset_type,
        'asset_id': int(asset_id),
        'operation': 'created' if before_row is None else 'updated',
        'before': before_row,
        'after': after_row,
    })


def _build_asset_mutations(old: dict, new: dict, update: dict) -> list[dict]:
    """从结算前后数据库快照生成稳定 ID 的资产修改审计。"""
    mutations: list[dict] = []

    new_trait_names = normalized_member_new_trait_names(
        update.get('new_traits'),
        member_name=str(old.get('name') or '').strip() or None,
    )
    old_trait_rows = old.get('trait_rows') or {}
    for trait_id, after_row in sorted((new.get('trait_rows') or {}).items()):
        if str((after_row or {}).get('name') or '').strip() not in new_trait_names:
            continue
        _append_asset_mutation(
            mutations,
            asset_type='trait',
            asset_id=trait_id,
            before_row=old_trait_rows.get(trait_id),
            after_row=after_row,
        )

    seen_skill_ids: set[int] = set()
    for skill_update in update.get('skill_updates') or []:
        if not isinstance(skill_update, dict):
            continue
        skill_id = int(skill_update.get('skill_id') or 0)
        if skill_id <= 0 or skill_id in seen_skill_ids:
            continue
        seen_skill_ids.add(skill_id)
        _append_asset_mutation(
            mutations,
            asset_type='skill',
            asset_id=skill_id,
            before_row=(old.get('skill_rows') or {}).get(skill_id),
            after_row=(new.get('skill_rows') or {}).get(skill_id),
        )

    seen_equipment_ids: set[int] = set()
    for equipment_update in update.get('equipment_updates') or []:
        if not isinstance(equipment_update, dict):
            continue
        equipment_id = int(equipment_update.get('equipment_id') or 0)
        if equipment_id <= 0 or equipment_id in seen_equipment_ids:
            continue
        seen_equipment_ids.add(equipment_id)
        _append_asset_mutation(
            mutations,
            asset_type='equipment',
            asset_id=equipment_id,
            before_row=(old.get('equipment_rows') or {}).get(equipment_id),
            after_row=(new.get('equipment_rows') or {}).get(equipment_id),
        )

    return mutations


def capture_s1_settlement_state(
    member_ids: list[int],
    *,
    include_gold: bool = True,
) -> dict[int, dict]:
    """批量读取成员结算相关权威表，避免逐成员查询。"""
    ids = sorted({int(member_id) for member_id in member_ids if member_id is not None})
    if not ids:
        return {}

    state = {
        member_id: {
            'name': '',
            **({'gold': Decimal('0.00')} if include_gold else {}),
            'character_class': '',
            'status': '',
            'traits': set(),
            'trait_rows': {},
            'skills': set(),
            'skill_rows': {},
            'items': {},
            'equipment_items': {},
            'item_quantities': {},
            'equipment_item_quantities': {},
            'equipment_rows': {},
        }
        for member_id in ids
    }
    main_columns = [
        AdventurerS1.id,
        AdventurerS1.name,
        AdventurerS1.character_class,
        AdventurerS1.status,
    ]
    if include_gold:
        main_columns.append(AdventurerS1.gold)
    for row in (
        db_manager.query(*main_columns)
        .filter(AdventurerS1.id.in_(ids))
        .all()
    ):
        member_id, name, character_class, status = row[:4]
        state[member_id]['name'] = str(name or '').strip()
        state[member_id]['character_class'] = str(character_class or '').strip()
        state[member_id]['status'] = str(status or '').strip()
        if include_gold:
            state[member_id]['gold'] = _money(row[4])

    if include_gold:
        for member_id, amount in (
            db_manager.query(AdventurerS1Currency.adventurer_id, AdventurerS1Currency.amount)
            .filter(
                AdventurerS1Currency.adventurer_id.in_(ids),
                AdventurerS1Currency.currency_code == 'gold',
            )
            .all()
        ):
            state[member_id]['gold'] = _money(amount)

    for (
        trait_id,
        member_id,
        name,
        description,
        description_ai,
        description_ai_at,
        description_ai_source_hash,
        lore_entries,
        is_archived,
    ) in (
        db_manager.query(
            AdventurerS1Trait.id,
            AdventurerS1Trait.adventurer_id,
            AdventurerS1Trait.name,
            AdventurerS1Trait.description,
            AdventurerS1Trait.description_ai,
            AdventurerS1Trait.description_ai_at,
            AdventurerS1Trait.description_ai_source_hash,
            AdventurerS1Trait.lore_entries,
            AdventurerS1Trait.is_archived,
        )
        .filter(AdventurerS1Trait.adventurer_id.in_(ids))
        .all()
    ):
        clean = str(name or '').strip()
        if not clean:
            continue
        archived = bool(is_archived)
        if not archived:
            state[member_id]['traits'].add(clean)
        state[member_id]['trait_rows'][int(trait_id)] = {
            'name': clean,
            'desc': str(description or '').strip(),
            'description_ai': (
                str(description_ai).strip() if description_ai is not None else None
            ),
            'description_ai_at': _iso_timestamp(description_ai_at),
            'description_ai_source_hash': (
                str(description_ai_source_hash).strip()
                if description_ai_source_hash is not None
                else None
            ),
            'lore_entries': list(lore_entries or []),
            'is_archived': archived,
        }

    for skill_id, member_id, name, description, equipped, recovery_minutes in (
        db_manager.query(
            AdventurerS1Skill.id,
            AdventurerS1Skill.adventurer_id,
            AdventurerS1Skill.name,
            AdventurerS1Skill.description,
            AdventurerS1Skill.equipped,
            AdventurerS1Skill.recovery_minutes,
        )
        .filter(AdventurerS1Skill.adventurer_id.in_(ids))
        .all()
    ):
        clean = str(name or '').strip()
        if clean:
            state[member_id]['skills'].add(clean)
            state[member_id]['skill_rows'][int(skill_id)] = {
                'name': clean,
                'desc': str(description or '').strip(),
                'equipped': bool(equipped),
                'recovery_minutes': max(0, int(recovery_minutes or 0)),
            }

    item_totals: dict[tuple[int, str], int] = defaultdict(int)
    equipment_item_totals: dict[tuple[int, str], int] = defaultdict(int)
    item_quantity_totals: dict[tuple[int, str], int] = defaultdict(int)
    equipment_item_quantity_totals: dict[tuple[int, str], int] = defaultdict(int)
    for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
        for (
            item_id,
            member_id,
            name,
            item_type,
            rarity,
            custom_desc,
            quantity,
            uses_left,
            evolution_stage,
            recovery_minutes,
        ) in (
            db_manager.query(
                model.id,
                model.adventurer_id,
                model.name,
                model.type,
                model.rarity,
                model.custom_desc,
                model.quantity,
                model.uses_left,
                model.evolution_stage,
                model.recovery_minutes,
            )
            .filter(model.adventurer_id.in_(ids))
            .all()
        ):
            clean = str(name or '').strip()
            if clean:
                item_units = _item_units(quantity, uses_left)
                item_totals[(member_id, clean)] += item_units
                item_quantity_totals[(member_id, clean)] += _positive_int(quantity)
                if model is AdventurerS1Equipment:
                    equipment_item_totals[(member_id, clean)] += item_units
                    equipment_item_quantity_totals[(member_id, clean)] += (
                        _positive_int(quantity)
                    )
                    state[member_id]['equipment_rows'][int(item_id)] = {
                        'name': clean,
                        'type': str(item_type or '').strip(),
                        'rarity': str(rarity or 'common').strip(),
                        'desc': str(custom_desc or '').strip(),
                        'quantity': _positive_int(quantity),
                        'uses_left': uses_left,
                        'evolution_stage': max(0, int(evolution_stage or 0)),
                        'recovery_minutes': max(0, int(recovery_minutes or 0)),
                        'units': _item_units(quantity, uses_left),
                    }
    for (member_id, name), quantity in item_totals.items():
        state[member_id]['items'][name] = quantity
    for (member_id, name), quantity in equipment_item_totals.items():
        state[member_id]['equipment_items'][name] = quantity
    for (member_id, name), quantity in item_quantity_totals.items():
        state[member_id]['item_quantities'][name] = quantity
    for (member_id, name), quantity in equipment_item_quantity_totals.items():
        state[member_id]['equipment_item_quantities'][name] = quantity
    return state


def build_s1_settlement_receipt(
    before: dict[int, dict],
    after: dict[int, dict],
    updates: dict[int, dict],
    gold_balances_after_write: dict[int, float],
    *,
    exact_asset_names: dict[int, dict[str, set[str]]] | None = None,
    party_transfers: dict[str, tuple] | None = None,
) -> dict:
    """按最终 member_updates 核对写入结果；死亡资产清理在本回执通过后执行。"""
    mismatches: list[dict] = []
    members: list[dict] = []
    for member_id in sorted(updates):
        update = updates.get(member_id) or {}
        old = before.get(member_id) or {}
        new = after.get(member_id) or {}
        exact_names = (
            (exact_asset_names or {}).get(member_id) or {}
        )
        checked: list[str] = []

        gold_change = _money(update.get('gold_change', 0))
        if gold_change != Decimal('0.00'):
            checked.append('gold')
            actual_gold = _money(new.get('gold'))
            if member_id not in gold_balances_after_write:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': 'gold',
                    'expected': 'currency_service_balance',
                    'actual': float(actual_gold),
                })
            else:
                expected_gold = _money(gold_balances_after_write[member_id])
                if actual_gold != expected_gold:
                    mismatches.append({
                        'adventurer_id': member_id,
                        'field': 'gold',
                        'expected': float(expected_gold),
                        'actual': float(actual_gold),
                    })

        raw_new_class = update.get('new_class')
        new_class = (
            ' '.join(raw_new_class.split())
            if isinstance(raw_new_class, str)
            else ''
        )
        if len(new_class) > 20:
            new_class = ''
        if new_class:
            checked.append('character_class')
            actual_class = str(new.get('character_class') or '').strip()
            if actual_class != new_class:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': 'character_class',
                    'expected': new_class,
                    'actual': actual_class,
                })

        settlement_marks_dead = False
        remaining_hp = update.get('remaining_hp')
        if remaining_hp is not None:
            try:
                settlement_marks_dead = int(remaining_hp) <= 0
            except (TypeError, ValueError):
                settlement_marks_dead = False
            if settlement_marks_dead:
                checked.append('death_status')
                actual_status = str(new.get('status') or '').strip()
                if actual_status != 'dead':
                    mismatches.append({
                        'adventurer_id': member_id,
                        'field': 'status',
                        'expected': 'dead',
                        'actual': actual_status,
                    })

        expected_traits = set(old.get('traits') or set())
        new_trait_names = normalized_member_new_trait_names(
            update.get('new_traits'),
            member_name=str(old.get('name') or '').strip() or None,
        )
        for removed_name in parse_member_removed_trait_names(update.get('removed_traits')):
            if removed_name in new_trait_names:
                continue
            resolved_name = resolve_removed_trait_db_name(removed_name, expected_traits)
            if resolved_name:
                expected_traits.discard(resolved_name)
        expected_traits.update(new_trait_names)
        if update.get('removed_traits') or update.get('new_traits'):
            checked.append('traits')
            actual_traits = set(new.get('traits') or set())
            ignored_trait_names = set(exact_names.get('trait') or set())
            if (
                actual_traits - ignored_trait_names
                != expected_traits - ignored_trait_names
            ):
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': 'traits',
                    'expected': sorted(
                        expected_traits - ignored_trait_names
                    ),
                    'actual': sorted(
                        actual_traits - ignored_trait_names
                    ),
                })

        expected_skills = set(old.get('skills') or set())
        expected_skills.difference_update(_names(update.get('removed_skills')))
        expected_skills.update(_normalized_skill_names(update.get('new_skills')))
        for skill_update in update.get('skill_updates') or []:
            if not isinstance(skill_update, dict):
                continue
            skill_id = int(skill_update.get('skill_id') or 0)
            before_row = (old.get('skill_rows') or {}).get(skill_id)
            after_row = (new.get('skill_rows') or {}).get(skill_id)
            expected_row = {
                'name': str(skill_update.get('name') or '').strip(),
                'desc': str(skill_update.get('desc') or '').strip(),
                'equipped': bool((before_row or {}).get('equipped', True)),
                'recovery_minutes': max(
                    0, int(skill_update.get('recovery_minutes') or 0),
                ),
            }
            checked.append(f'skill_update:{skill_id}')
            if before_row is None or after_row != expected_row:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': f'skill_updates.{skill_id}',
                    'expected': expected_row,
                    'actual': after_row,
                })
            if before_row is not None:
                expected_skills.discard(str(before_row.get('name') or '').strip())
            if expected_row['name']:
                expected_skills.add(expected_row['name'])
        if (
            update.get('removed_skills')
            or update.get('new_skills')
            or update.get('skill_updates')
        ):
            checked.append('skills')
            actual_skills = set(new.get('skills') or set())
            ignored_skill_names = set(exact_names.get('skill') or set())
            if (
                actual_skills - ignored_skill_names
                != expected_skills - ignored_skill_names
            ):
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': 'skills',
                    'expected': sorted(
                        expected_skills - ignored_skill_names
                    ),
                    'actual': sorted(
                        actual_skills - ignored_skill_names
                    ),
                })

        equipment_update_unit_deltas: dict[str, int] = defaultdict(int)
        equipment_update_asset_quantity_deltas: dict[str, int] = defaultdict(int)
        for equipment_update in update.get('equipment_updates') or []:
            if not isinstance(equipment_update, dict):
                continue
            equipment_id = int(equipment_update.get('equipment_id') or 0)
            before_row = (old.get('equipment_rows') or {}).get(equipment_id)
            after_row = (new.get('equipment_rows') or {}).get(equipment_id)
            expected_row = {
                'name': str(equipment_update.get('name') or '').strip(),
                'type': str(equipment_update.get('type') or '').strip(),
                'rarity': str(equipment_update.get('rarity') or '').strip(),
                'desc': str(equipment_update.get('desc') or '').strip(),
                'quantity': int((before_row or {}).get('quantity') or 1),
                'uses_left': equipment_update.get('uses_left'),
                'evolution_stage': int(equipment_update.get('evolution_stage') or 0),
                'recovery_minutes': max(
                    0, int(equipment_update.get('recovery_minutes') or 0),
                ),
                'units': _item_units(
                    (before_row or {}).get('quantity', 1),
                    equipment_update.get('uses_left'),
                ),
            }
            checked.append(f'equipment_update:{equipment_id}')
            if before_row is None or after_row != expected_row:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': f'equipment_updates.{equipment_id}',
                    'expected': expected_row,
                    'actual': after_row,
                })
            if before_row is not None:
                before_name = str(before_row.get('name') or '').strip()
                after_name = expected_row['name']
                if before_name:
                    equipment_update_unit_deltas[before_name] -= int(
                        before_row.get('units')
                        or _item_units(
                            before_row.get('quantity', 1),
                            before_row.get('uses_left'),
                        )
                    )
                    equipment_update_asset_quantity_deltas[before_name] -= int(
                        before_row.get('quantity') or 1
                    )
                if after_name:
                    equipment_update_unit_deltas[after_name] += int(
                        expected_row['units']
                    )
                    equipment_update_asset_quantity_deltas[after_name] += int(
                        expected_row['quantity']
                    )

        granted = _named_entries(update.get('new_equipments'))
        granted_asset_quantities = _named_asset_quantities(update.get('new_equipments'))
        consumed = _removed_entries(update.get('consumed_equipments'))
        for item_name, transfer in (party_transfers or {}).items():
            sender_id, _receiver_id, source_id, _source_quantity = transfer[:4]
            if int(sender_id) != member_id:
                continue
            source_before = (old.get('equipment_rows') or {}).get(int(source_id))
            if source_before is not None:
                consumed[item_name] = int(source_before.get('units') or 1)
        equipment_update_names = {
            name
            for name, delta in equipment_update_unit_deltas.items()
            if delta
        } | {
            name
            for name, delta in equipment_update_asset_quantity_deltas.items()
            if delta
        }
        for name in sorted(set(granted) | set(consumed) | equipment_update_names):
            checked.append(f'item:{name}')
            old_quantity = int((old.get('items') or {}).get(name, 0))
            equipment_items = old.get('equipment_items')
            if isinstance(equipment_items, dict):
                departure_equipment_quantity = min(
                    old_quantity,
                    max(0, int(equipment_items.get(name, 0))),
                )
                # 回执核对的是本场变化，尚未执行死亡没收；所有成员的出发前
                # 仓库库存都不能被随身 consumed 扣减。
                protected_warehouse_quantity = max(
                    0, old_quantity - departure_equipment_quantity,
                )
                expected_quantity = protected_warehouse_quantity + max(
                    0,
                    departure_equipment_quantity
                    + equipment_update_unit_deltas.get(name, 0)
                    + granted.get(name, 0)
                    - consumed.get(name, 0),
                )
            else:
                # 兼容旧的手工回执快照；生产捕获始终提供 equipment_items。
                expected_quantity = max(
                    0,
                    old_quantity
                    + equipment_update_unit_deltas.get(name, 0)
                    + granted.get(name, 0)
                    - consumed.get(name, 0),
                )
            actual_quantity = int((new.get('items') or {}).get(name, 0))
            item_units_match = actual_quantity == expected_quantity
            if not item_units_match and 'item_quantities' in old and 'item_quantities' in new:
                old_asset_quantities = old.get('item_quantities') or {}
                available_asset_quantity = int(
                    old_asset_quantities.get(name, 0)
                ) + equipment_update_asset_quantity_deltas.get(
                    name, 0,
                ) + granted_asset_quantities.get(name, 0)
                expected_asset_quantity = max(
                    0,
                    available_asset_quantity - consumed.get(name, 0),
                )
                actual_asset_quantity = int((new.get('item_quantities') or {}).get(name, 0))
                item_units_match = (
                    available_asset_quantity > 1
                    and 0 < actual_asset_quantity < available_asset_quantity
                    and actual_quantity > actual_asset_quantity
                    and actual_asset_quantity == expected_asset_quantity
                )
            if not item_units_match:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': f'items.{name}',
                    'expected': expected_quantity,
                    'actual': actual_quantity,
                })

        for item_name, transfer in (party_transfers or {}).items():
            sender_id, _receiver_id, source_id, source_quantity = transfer[:4]
            if int(sender_id) != member_id:
                continue
            source_id = int(source_id)
            source_quantity = int(source_quantity)
            checked.append(f'transfer_source:{source_id}')
            source_before = (old.get('equipment_rows') or {}).get(source_id)
            if (
                source_before is None
                or source_before.get('name') != item_name
                or int(source_before.get('quantity') or 0) != source_quantity
            ):
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': f'transfer_source.{source_id}.before',
                    'expected': {
                        'name': item_name,
                        'quantity': source_quantity,
                    },
                    'actual': source_before,
                })
            source_after = (new.get('equipment_rows') or {}).get(source_id)
            if source_after is not None:
                mismatches.append({
                    'adventurer_id': member_id,
                    'field': f'transfer_source.{source_id}',
                    'expected': 'removed',
                    'actual': source_after,
                })

        members.append({
            'adventurer_id': member_id,
            'status': 'passed' if not any(
                item['adventurer_id'] == member_id for item in mismatches
            ) else 'mismatch',
            'checked': checked,
            'asset_mutations': _build_asset_mutations(old, new, update),
        })

    return {
        'schema_version': 2,
        'stage': 'member_update_flush',
        'status': 'passed' if not mismatches else 'mismatch',
        'member_count': len(updates),
        'members': members,
        'mismatches': mismatches[:50],
    }


def verify_s1_settlement_receipt(
    before: dict[int, dict],
    updates: dict[int, dict],
    gold_balances_after_write: dict[int, float],
    *,
    exact_asset_names: dict[int, dict[str, set[str]]] | None = None,
    party_transfers: dict[str, tuple] | None = None,
) -> dict:
    """flush 后重读权威表；不一致时抛错，交由外层事务整体回滚。"""
    ok, err = db_manager.flush()
    if not ok:
        raise RuntimeError(f'S1 结算到账回执 flush 失败: {err}')
    after = capture_s1_settlement_state(list(updates))
    receipt = build_s1_settlement_receipt(
        before,
        after,
        updates,
        gold_balances_after_write,
        exact_asset_names=exact_asset_names,
        party_transfers=party_transfers,
    )
    if receipt['status'] != 'passed':
        raise S1SettlementReceiptMismatch(receipt)
    return receipt
