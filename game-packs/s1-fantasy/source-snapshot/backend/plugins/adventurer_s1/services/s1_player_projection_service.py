"""Player-only presentation projections; never use these for settlement or AI input."""


def _scalar_fields(value: dict, fields: tuple[str, ...]) -> dict:
    return {
        key: value[key] for key in fields
        if key in value and isinstance(value[key], (str, int, float, bool, type(None)))
    }


def project_building_result(result: object) -> dict | None:
    """Shared whitelist for the owner's activity history and completion popup.

    Keep the persisted result intact. Unknown fields, raw snapshots, review evidence,
    worker diagnostics and nested additions never reach the browser.
    """
    if not isinstance(result, dict):
        return None
    out = _scalar_fields(result, (
        'type', 'success', 'narrative', 'gold_refunded', 'stamina_refunded',
        'system_abort', 'operation', 'direction_fit', 'reason',
        'old_name', 'new_name', 'new_desc', 'uses_left', 'is_edible',
        'cookware_name', 'consumed', 'unreasonable_input', 'items_preserved',
        'pet_preserved', 'evolution_stage', 'auxiliary_material_consumed',
        'auxiliary_material_name', 'carried_item_name', 'item_consumed',
    ))
    for key in ('healed_traits', 'ingredient_names', 'consumed_item_names'):
        if isinstance(result.get(key), list):
            out[key] = [value for value in result[key] if isinstance(value, str)]
    for key in ('recovery_traits', 'modified_traits', 'consumed_items'):
        if not isinstance(result.get(key), list):
            continue
        fields = (
            ('slot_id', 'position', 'name') if key == 'consumed_items' else
            ('name', 'new_name', 'new_desc', 'policy', 'stage', 'remaining_cycles')
        )
        out[key] = [_scalar_fields(value, fields) for value in result[key] if isinstance(value, dict)]
    for key in ('skill', 'old_skill'):
        if isinstance(result.get(key), dict):
            out[key] = _scalar_fields(result[key], ('id', 'name', 'description', 'equipped'))
    if isinstance(result.get('undo'), dict):
        # The activity badge must distinguish a reverted result from a live success.
        out['undo'] = _scalar_fields(result['undo'], ('kind', 'reverted_at'))
    if isinstance(result.get('ai_feedback'), dict):
        out['ai_feedback'] = _scalar_fields(result['ai_feedback'], (
            'target_type', 'target_id', 'content_label', 'quality_score', 'preference_score',
        ))
    if result.get('type') == 'pet_evolution':
        snapshots = result.get('_input_items')
        projected_inputs = result.get('input_items')
        already_projected = not isinstance(snapshots, list) and isinstance(projected_inputs, list)
        if already_projected:
            snapshots = projected_inputs
        if not isinstance(snapshots, list):
            before = result.get('pre_evolution_snapshot')
            snapshots = [before if isinstance(before, dict) else {'name': result.get('old_name')}]
            if result.get('auxiliary_material_name'):
                snapshots.append({'name': result['auxiliary_material_name']})
        inputs = []
        for index, snapshot in enumerate(snapshots[:2]):
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get('name'), str):
                continue
            if not snapshot['name'].strip():
                continue
            role = snapshot.get('role') if already_projected else ('primary' if index == 0 else 'auxiliary')
            if role not in ('primary', 'auxiliary'):
                continue
            item = {'role': role, 'name': snapshot['name']}
            desc = (snapshot.get('description') if already_projected else
                    snapshot.get('desc') or snapshot.get('custom_desc'))
            if isinstance(desc, str) and desc.strip():
                item['description'] = desc
            inputs.append(item)
        out['input_items'] = inputs
    return out
