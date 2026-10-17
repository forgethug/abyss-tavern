"""Shared candidate contract for fully consumed S1 expedition assets."""

from __future__ import annotations


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_consumed_asset_continuity_review_context(
    result_data: dict,
    audit_member_snapshot: list[dict] | None,
) -> list[dict]:
    """Build exact whole-asset continuity candidates from final settlement state."""
    held_by_id: dict[int, dict[str, list[dict]]] = {}
    available_by_id: dict[int, dict[str, int]] = {}
    for member in audit_member_snapshot or []:
        if not isinstance(member, dict):
            continue
        try:
            member_id = int(member.get('adventurer_id'))
        except (TypeError, ValueError):
            continue
        rows: dict[str, list[dict]] = {}
        quantities: dict[str, int] = {}
        for item in member.get('equipment') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            quantity = max(1, _coerce_int(item.get('quantity'), 1))
            uses_left = item.get('uses_left')
            available_units = quantity
            if quantity == 1 and uses_left is not None:
                available_units = max(1, _coerce_int(uses_left, 1))
            rows.setdefault(name, []).append({
                'location': 'equipment',
                'type': item.get('type'),
                'rarity': item.get('rarity'),
                'quantity': quantity,
                'uses_left': uses_left,
                'evolution_stage': max(
                    0, _coerce_int(item.get('evolution_stage'), 0),
                ),
                'desc': item.get('description') or item.get('desc'),
            })
            quantities[name] = quantities.get(name, 0) + available_units
        held_by_id[member_id] = rows
        available_by_id[member_id] = quantities

    candidates: list[dict] = []
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        member_id = _coerce_int(update.get('id'), -1)
        consumed_quantities: dict[str, int] = {}
        for item in update.get('consumed_equipments') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            consumed_quantities[name] = (
                consumed_quantities.get(name, 0)
                + max(1, _coerce_int(item.get('quantity'), 1))
            )
        proposed_new = update.get('new_equipments') or {}
        if not isinstance(proposed_new, dict):
            proposed_new = {}
        for name, consumed_quantity in consumed_quantities.items():
            available_quantity = available_by_id.get(member_id, {}).get(name)
            instances = held_by_id.get(member_id, {}).get(name)
            if (
                not instances
                or available_quantity is None
                or consumed_quantity < available_quantity
            ):
                continue
            candidates.append({
                'adventurer_id': member_id,
                'consumed_name': name,
                'consumed_quantity': consumed_quantity,
                'departure_instances': instances,
                'proposed_new_names': [str(item_name) for item_name in proposed_new],
            })
    return candidates
