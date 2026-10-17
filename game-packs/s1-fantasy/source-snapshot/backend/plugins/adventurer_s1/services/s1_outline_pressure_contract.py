"""Pressure references share already validated encounter capabilities."""

COVERAGE_MODES = frozenset({'shared', 'individual', 'linked', 'propagating'})
RESOLUTION_MODES = frozenset({'hp_damage', 'durability_bypass'})
REFERENCE_KEYS = frozenset({
    'pressure_index', 'coverage_mode', 'capability_refs', 'resolution_modes',
})
LEGACY_KEYS = frozenset({
    'pressure_index', 'coverage_mode', 'contact_rule',
    'local_change_limit', 'residual_rule', 'resolution_paths',
})
LEGACY_PATH_KEYS = frozenset({'mode', 'trigger', 'effect_boundary', 'prevention_boundary'})

PRESSURE_STRUCTURE_OUTPUT_SHAPE = """【压力结构字段】
以下为字段类型说明，各结构的字段均须完整输出；不是剧情示例或默认取值。lethal_pressure_structures 中每项只采用其来源对应的结构：
有主体画像：{pressure_index:非负整数, coverage_mode:覆盖类型, capability_refs:非空数组<主体能力引用>, resolution_modes:长度1~2的去重数组<生命损伤类型>}。
主体能力引用：新画像为 {profile_index:非负整数, capability_indexes:非空去重数组<非负整数>}；获准直接参与的长期角色为 {world_actor_id:正整数, capability_indexes:非空去重数组<非负整数>}。
无主体画像的既有环境压力：{pressure_index:非负整数, coverage_mode:覆盖类型, contact_rule:非空字符串, local_change_limit:非空字符串, residual_rule:非空字符串, resolution_paths:长度1~2的数组<作用路径>}。
作用路径：{mode:生命损伤类型, trigger:非空字符串, effect_boundary:非空字符串, prevention_boundary:字符串}，各路径的 mode 不重复。
覆盖类型为 shared / individual / linked / propagating；生命损伤类型为 hp_damage / durability_bypass，按原作用定义选择。能力引用结构中，capability_refs 确定能力来源，resolution_modes 确定生命损伤类型，两者分别必填。"""


def _index(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            value = int(value)
        except ValueError:
            return None
    return value if type(value) is int and value >= 0 else None


def normalize_pressure_structure(value):
    """Project either current references or legacy text; never infer effects."""
    if not isinstance(value, dict):
        return None, 'fields'
    is_reference = 'capability_refs' in value or 'resolution_modes' in value
    keys = REFERENCE_KEYS if is_reference else LEGACY_KEYS
    if not keys.issubset(value):
        return None, 'fields'
    pressure_index = _index(value.get('pressure_index'))
    if pressure_index is None:
        return None, 'pressure_index'
    coverage = value.get('coverage_mode')
    if not isinstance(coverage, str) or coverage not in COVERAGE_MODES:
        return None, 'coverage_mode'
    result = {'pressure_index': pressure_index, 'coverage_mode': coverage}
    if is_reference:
        modes = value['resolution_modes']
        if (not isinstance(modes, list) or not 1 <= len(modes) <= 2
                or any(not isinstance(mode, str) or mode not in RESOLUTION_MODES for mode in modes)
                or len(set(modes)) != len(modes)):
            return None, 'resolution_modes'
        refs = value['capability_refs']
        # A singleton object has the same meaning as a one-element reference array.
        # Keep source selection, required indexes and later authority binding strict.
        if isinstance(refs, dict):
            refs = [refs]
        if not isinstance(refs, list) or not refs:
            return None, 'capability_refs'
        normalized = []
        for ref in refs:
            if not isinstance(ref, dict):
                return None, 'capability_refs'
            selectors = [key for key in ('profile_index', 'world_actor_id') if key in ref]
            if len(selectors) != 1:
                return None, 'capability_refs'
            key = selectors[0]
            source = _index(ref[key])
            indexes = ref.get('capability_indexes')
            if source is None or (key == 'world_actor_id' and source == 0):
                return None, 'capability_refs'
            if not isinstance(indexes, list) or not indexes:
                return None, 'capability_refs'
            indexes = [_index(index) for index in indexes]
            if any(index is None for index in indexes) or len(set(indexes)) != len(indexes):
                return None, 'capability_refs'
            normalized.append({key: source, 'capability_indexes': indexes})
        result.update(capability_refs=normalized, resolution_modes=list(modes))
        return result, None
    for key in ('contact_rule', 'local_change_limit', 'residual_rule'):
        if not isinstance(value[key], str) or not value[key].strip():
            return None, key
        result[key] = value[key]
    paths = value['resolution_paths']
    if not isinstance(paths, list) or not 1 <= len(paths) <= 2:
        return None, 'resolution_paths'
    result['resolution_paths'] = []
    seen = set()
    for path in paths:
        if not isinstance(path, dict) or not LEGACY_PATH_KEYS.issubset(path):
            return None, 'resolution_paths'
        mode = path['mode']
        if not isinstance(mode, str) or mode not in RESOLUTION_MODES or mode in seen:
            return None, 'resolution_paths'
        for key in ('trigger', 'effect_boundary', 'prevention_boundary'):
            if not isinstance(path[key], str) or (key != 'prevention_boundary' and not path[key].strip()):
                return None, 'resolution_paths'
        seen.add(mode)
        result['resolution_paths'].append({key: path[key].strip() for key in LEGACY_PATH_KEYS})
    return result, None


def bind_pressure_capability_refs(structures, profiles, original_to_valid):
    """Bind request-local references after identity/authority/profile validation.

    Invalid profiles may have been dropped. Do not let their old indexes silently
    point at another entity. World actors must already be inherited through the
    validated direct-use path. Persist final new-profile indexes and actor IDs.
    """
    actors = {}
    for index, profile in enumerate(profiles):
        actor_id = profile.get('world_actor_id')
        if actor_id is not None:
            actors.setdefault(actor_id, []).append(index)
    bound = []
    for structure_index, structure in enumerate(structures):
        if 'capability_refs' not in structure:
            bound.append(structure)
            continue
        references = []
        seen = set()
        for ref_index, ref in enumerate(structure['capability_refs']):
            path = f'lethal_pressure_structures[{structure_index}].capability_refs[{ref_index}]'
            if 'profile_index' in ref:
                index = original_to_valid.get(ref['profile_index'])
                if index is None or profiles[index].get('world_actor_id') is not None:
                    return None, path
            else:
                matches = actors.get(ref['world_actor_id'], [])
                if len(matches) != 1:
                    return None, path
                index = matches[0]
            capabilities = profiles[index]['capabilities']
            for capability_index in ref['capability_indexes']:
                pair = (index, capability_index)
                if capability_index >= len(capabilities) or pair in seen:
                    return None, path
                seen.add(pair)
            selector = ({'profile_index': index} if 'profile_index' in ref
                        else {'world_actor_id': ref['world_actor_id']})
            references.append({**selector, 'capability_indexes': list(ref['capability_indexes'])})
        bound.append({**structure, 'capability_refs': references})
    return bound, None


PRESSURE_REFERENCE_READING_RULE = """【压力能力引用】
压力结构中的 capability_refs 按 profile_index 指向同份大纲 encounter_capability_profiles 的零基位置，或按 world_actor_id 选择其中同一准确 ID 的已继承画像；capability_indexes 指向该主体 capabilities 的零基位置。读取被引用的原能力及该主体完整限制与当前状态，作用对象、接触条件、范围和持续性以这些定义为准；引用不新增能力或证明接触已经发生。resolution_modes 只标记原作用的生命损伤类型：hp_damage 为普通生命损伤，durability_bypass 为已有独立不可逆机制。实际防御、局部变化与剩余作用在交锋中逐次判定；未改变的本领保持，不因引用只列部分能力而失去其它本领。"""


def has_pressure_capability_refs(outline):
    if not isinstance(outline, dict):
        return False
    structures = outline.get('lethal_pressure_structures')
    return isinstance(structures, list) and any(
        isinstance(item, dict) and 'capability_refs' in item for item in structures
    )
