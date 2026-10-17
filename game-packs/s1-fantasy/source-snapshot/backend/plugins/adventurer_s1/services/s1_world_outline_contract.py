"""Pure output-contract helpers for S1 public-world auxiliary AI fields."""
from __future__ import annotations

import math
from typing import Any

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


MAX_WORLD_FACT_VALUE_CHARS = 2000
MAX_WORLD_FACT_CONTAINER_ITEMS = 30
DEFAULT_WORLD_ACTOR_MAX_AI_UPDATES = 3
WORLD_FACT_CHANGE_KINDS = frozenset({'new', 'advance', 'close', 'resolve', 'successor'})
WORLD_STORY_THREAD_TYPES = frozenset({
    'entity', 'event', 'faction', 'location', 'omen', 'artifact',
})
WORLD_STORY_THREAD_STATUSES = frozenset({
    'seeded', 'active', 'escalated', 'closing', 'dormant', 'resolved',
})
WORLD_ACTOR_TYPES = frozenset({'individual', 'collective', 'manifestation', 'other'})
WORLD_ACTOR_STATUSES = frozenset({
    'rumored', 'active', 'withdrawn', 'missing', 'retired', 'dead',
})
WORLD_INTEL_ALL_DUNGEONS_SCOPE = '全部地牢'
MAX_FACTION_PROFILE_UPDATE_ITEMS = 16
MAX_FACTION_PROFILE_TEXT_CHARS = 600
MAX_FACTION_PROFILE_DB_INTEGER = 2_147_483_647
FACTION_PROFILE_STATUSES = frozenset({'rumored', 'active', 'dormant', 'dissolved'})
FACTION_PROFILE_VISIBILITIES = frozenset({'public', 'rumor', 'restricted'})
FACTION_PROFILE_FACETS = frozenset({
    'scale', 'territory', 'operating_range', 'history', 'governance',
    'internal_structure', 'capabilities', 'knowledge', 'resources',
    'culture', 'current_agenda', 'current_state',
})
FACTION_PROFILE_THREAD_ROLES = frozenset({'core', 'involved', 'historical'})
FACTION_PRESENCE_RECEIPTS_FIELD = 'faction_presence_receipts'
FACTION_PRESENCE_REVIEW_SCOPE_FIELD = 'faction_presence_review'

_WORLD_STORY_THREAD_ALLOWED_FIELDS = frozenset({
    'id', 'thread_id', 'predecessor_id', 'title', 'thread_type', 'type',
    'status', 'scope_keys', 'scope', 'trigger_keywords', 'summary',
    'next_reveal_hint', 'power_tier', 'power_profile', 'weight',
    'change_kind', 'source_world_story_thread_id', 'source_revision',
    'cycle_reseed', 'continuity_actor_id',
})
_WORLD_STORY_THREAD_CONTEXT_ONLY_FIELDS = frozenset({
    'base_dungeon', 'revision', 'closure_debt', 'injection_count',
    'missed_injection_runs', 'chain_key', 'match_score', 'updated_at',
})
_WORLD_STORY_THREAD_TEXT_FIELDS = frozenset({
    'title', 'summary', 'next_reveal_hint', 'power_tier', 'power_profile',
})
_WORLD_STORY_THREAD_TEXT_LIST_FIELDS = {
    'scope_keys': (8, 80),
    'trigger_keywords': (10, 50),
}
_WORLD_ACTOR_ALLOWED_FIELDS = frozenset({
    'id', 'canonical_name', 'name', 'aliases', 'actor_type', 'status',
    'identity_summary', 'stable_principles', 'power_tier', 'power_boundary',
    'capability_profile',
    'long_term_goal', 'current_agenda', 'current_state', 'scope_keys',
    'trigger_keywords', 'importance', 'faction_relations', 'thread_links',
    'interaction_summary', 'actor_change_summary', 'relation_change_summary',
})
_WORLD_ACTOR_THREAD_LINK_ALLOWED_FIELDS = frozenset({
    'thread_id', 'thread_title', 'involvement', 'narrative_role',
    'lease_required',
})
_WORLD_ACTOR_THREAD_INVOLVEMENTS = frozenset({'direct', 'indirect', 'historical'})
_WORLD_ACTOR_TEXT_FIELDS = frozenset({
    'canonical_name', 'name', 'identity_summary', 'stable_principles',
    'power_tier', 'power_boundary', 'long_term_goal', 'current_agenda',
    'current_state', 'interaction_summary', 'actor_change_summary',
    'relation_change_summary',
})
_WORLD_ACTOR_TEXT_LIST_FIELDS = {
    'aliases': (8, 120),
    'scope_keys': (8, 80),
    'trigger_keywords': (10, 50),
}
_WORLD_ACTOR_FACTION_RELATION_ALLOWED_FIELDS = frozenset({
    'faction_key', 'faction_name', 'public_stance', 'private_view',
    'interests', 'concerns', 'dependencies', 'unresolved_matters',
    'behavior_boundary', 'last_change_summary',
})
_WORLD_ACTOR_FACTION_RELATION_TEXT_FIELDS = (
    'faction_key', 'faction_name', 'public_stance', 'private_view',
    'interests', 'concerns', 'dependencies', 'unresolved_matters',
    'behavior_boundary', 'last_change_summary',
)
_WORLD_FACT_UPDATE_ALLOWED_FIELDS = frozenset({
    'summary', 'change_kind', 'source_world_story_thread_id', 'source_revision',
    'title', 'name', 'fact', 'entry', 'detail', 'description',
    'scope_keys', 'trigger_keywords', 'campaign_dispatch',
})
_WORLD_FACT_UPDATE_OPTIONAL_TEXT_FIELDS = frozenset({
    'title', 'name', 'fact', 'entry', 'detail', 'description',
})
_WORLD_FACT_UPDATE_TEXT_LIST_FIELDS = {
    'scope_keys': (8, 80),
    'trigger_keywords': (10, 50),
}
_FACTION_PROFILE_ALLOWED_FIELDS = frozenset({
    'id', 'source_revision', 'canonical_name', 'aliases', 'status',
    'identity_summary', 'scope_keys', 'trigger_keywords', 'importance',
    'facet_updates', 'thread_links', 'change_summary',
})
_FACTION_PROFILE_FACET_ALLOWED_FIELDS = frozenset({
    'facet', 'visibility', 'summary',
})
_FACTION_PROFILE_THREAD_LINK_ALLOWED_FIELDS = frozenset({
    'thread_id', 'thread_title', 'relation_role',
})
_MAINTENANCE_ALLOWED_FIELDS = frozenset({
    'dm_handoff_note', 'world_story_threads', 'world_actors',
    'faction_profile_updates', 'cycle_reseed_transition',
    'ecology_continuity_debt_resolutions', 'location_state_changes',
})

WORLD_LOCATION_MAINTENANCE_RULE = """
【地点当前态的局部演变】
authoritative_location_state_snapshot 是本轮维护的地点基线；其中 state_updated_at 是该状态最近保存时间，captured_at 是本轮时间，不等于某个生态过程的起始时间。
在 is_explorable=true 时，可依据地点当前生态条件、仍在作用的过程与有据的时间经过，向前推演已经能够自行形成的局部变化。已成立的结果与仍有效的限制继续继承；阶段终结不要求恢复原资源，也不意味着世界停止演变。条件不足、仍需外部投入或尚未发生的主动干预时保持原状。
这种限定的向前推演只写入可选 location_state_changes 数组，不扩展世界线、主体或势力的写权限，不复制到便签或其它对象中绕过地点审核。不要求新建世界线、安排采集、补库存或形成玩家收益；具体资源是否可取得仍由后续实际生态与行动决定。
数组只包含本轮确有变化且 _write_available=true 的已登记地点；省略或 [] 表示保留全部当前态，null 非法。最多为快照地点数，每个 location_id 至多一项。每项只含 location_id、location_name、expected_revision、new_state、change_summary；location_id 是 JSON 正整数，expected_revision 是 JSON 非负整数，其余三项是非空字符串。身份和版本逐字承接快照，new_state 是不超过 2000 字符的完整变化后现状，change_summary 是不超过 500 字符的真实变化原因。没有变化的地点不重复输出；未见证的后台变化不发布酒馆情报。
"""

WORLD_LOCATION_MAINTENANCE_READ_ONLY_RULE = """
【地点当前态的只读基线】
本轮 authoritative_location_state_snapshot 只提供已保存的地点当前态，地点自然演变不在本轮写入范围。输出沿用 maintenance_output_contract 中的字段；依据 new_candidates 保存已结算变化，其余地点现状继续继承。便签、世界线、主体和势力档案各按自身授权保存事实，不把本轮新推演的地点变化写入其中。
"""


def normalize_maintenance_location_changes(value: object, snapshot: dict | None) -> list[dict]:
    """Bind a complete maintenance delta to the frozen location identities."""
    from plugins.adventurer_s1.services.s1_world_location_state_contract import (
        normalize_location_state_changes,
    )

    if not isinstance(value, list):
        raise ValueError('maintenance_location_changes_not_array')
    if not value:
        return []
    if not isinstance(snapshot, dict) or snapshot.get('is_explorable') is not True:
        raise ValueError('maintenance_location_snapshot_unavailable')
    locations = {
        row['location_id']: row for row in snapshot.get('locations', [])
        if isinstance(row, dict) and type(row.get('location_id')) is int
    }
    fields = (
        'location_id', 'location_name', 'expected_revision', 'new_state', 'change_summary',
    )
    projected = [
        {key: raw[key] for key in fields if key in raw}
        if isinstance(raw, dict) else raw for raw in value
    ]
    changes, errors = normalize_location_state_changes(projected, max_changes=len(locations))
    if errors or len(changes) != len(value):
        raise ValueError('maintenance_location_changes_invalid:' + '; '.join(errors))
    for raw, change in zip(projected, changes):
        source = locations.get(change.get('location_id'))
        if (
            not source or source.get('_write_available') is not True
            or change.get('location_name') != source.get('location_name')
            or change['expected_revision'] != source.get('revision')
            or change['new_state'] == source.get('current_state')
            or any(
                not isinstance(raw.get(key), str)
                or raw[key].strip() != change.get(key)
                for key in ('location_name', 'new_state', 'change_summary')
            )
        ):
            raise ValueError('maintenance_location_identity_or_delta_invalid')
    return changes
_CYCLE_RESEED_TRANSITION_ALLOWED_FIELDS = frozenset({
    'location_state_change', 'dungeon_explorability_change',
})


def _normalize_singleton_record_container(
    value: object,
    *,
    field: str,
    identity_fields: frozenset[str],
    diagnostics: list[str],
) -> object:
    """Wrap one unmistakable record without treating an arbitrary object as a row."""
    if not isinstance(value, dict):
        return value
    if not any(str(key) in identity_fields for key in value):
        return value
    diagnostics.append(f'{field} 单对象已无损规范为单元素数组')
    return [value]


def _clean_json_value(
    value: Any,
    *,
    depth: int = 0,
    diagnostics: list[str] | None = None,
    path: str = 'value',
) -> Any:
    """Bound already-structured JSON without inferring any prose semantics."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        if diagnostics is not None:
            diagnostics.append(f'{path} 包含非有限数值，已丢弃')
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if len(cleaned) > MAX_WORLD_FACT_VALUE_CHARS and diagnostics is not None:
            diagnostics.append(
                f'{path} 超过 {MAX_WORLD_FACT_VALUE_CHARS} 字符，已截断'
            )
        return cleaned[:MAX_WORLD_FACT_VALUE_CHARS]
    if depth >= 3:
        if diagnostics is not None:
            diagnostics.append(f'{path} 嵌套超过 3 层，已丢弃')
        return None
    if isinstance(value, list):
        if len(value) > MAX_WORLD_FACT_CONTAINER_ITEMS and diagnostics is not None:
            diagnostics.append(
                f'{path} 超过 {MAX_WORLD_FACT_CONTAINER_ITEMS} 项，已丢弃超出部分'
            )
        return [
            cleaned
            for index, item in enumerate(value[:MAX_WORLD_FACT_CONTAINER_ITEMS])
            if (
                cleaned := _clean_json_value(
                    item,
                    depth=depth + 1,
                    diagnostics=diagnostics,
                    path=f'{path}[{index}]',
                )
            ) is not None
        ]
    if isinstance(value, dict):
        if len(value) > MAX_WORLD_FACT_CONTAINER_ITEMS and diagnostics is not None:
            diagnostics.append(
                f'{path} 超过 {MAX_WORLD_FACT_CONTAINER_ITEMS} 个字段，已忽略超出部分'
            )
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:MAX_WORLD_FACT_CONTAINER_ITEMS]:
            raw_key_text = str(raw_key).strip()
            if not isinstance(raw_key, str) and diagnostics is not None:
                diagnostics.append(f'{path} 含非字符串字段名，已转换')
            if len(raw_key_text) > 50 and diagnostics is not None:
                diagnostics.append(f'{path}.{raw_key_text[:50]} 字段名过长，已截断')
            key = raw_key_text[:50]
            if not key:
                if diagnostics is not None:
                    diagnostics.append(f'{path} 含空字段名，已忽略')
                continue
            normalized = _clean_json_value(
                raw_value,
                depth=depth + 1,
                diagnostics=diagnostics,
                path=f'{path}.{key}',
            )
            if normalized is not None:
                cleaned[key] = normalized
        return cleaned
    if diagnostics is not None:
        diagnostics.append(f'{path} 不是合法 JSON 值，已丢弃')
    return None


def normalize_world_fact_updates(value: object) -> tuple[list[dict], list[str]]:
    """Accept the canonical ``list[dict]`` contract and drop bad auxiliary rows."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['world_fact_updates 必须是对象数组，已丢弃非法容器']

    diagnostics: list[str] = []
    normalized: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            diagnostics.append(f'world_fact_updates[{index}] 不是对象，已丢弃')
            continue
        projected_item = {
            field: item[field]
            for field in _WORLD_FACT_UPDATE_ALLOWED_FIELDS
            if field in item
        }
        summary = projected_item.get('summary')
        if not isinstance(summary, str) or not summary.strip():
            diagnostics.append(
                f'world_fact_updates[{index}].summary 缺失或不是非空字符串，已丢弃'
            )
            continue
        cleaned: dict[str, Any] = {}
        summary_text = summary.strip()
        if len(summary_text) > MAX_WORLD_FACT_VALUE_CHARS:
            diagnostics.append(
                f'world_fact_updates[{index}].summary 超过 '
                f'{MAX_WORLD_FACT_VALUE_CHARS} 字符，已截断'
            )
        cleaned['summary'] = summary_text[:MAX_WORLD_FACT_VALUE_CHARS]

        if 'change_kind' not in projected_item:
            diagnostics.append(
                f'world_fact_updates[{index}].change_kind 缺失，已丢弃'
            )
            continue
        raw_change_kind = projected_item.get('change_kind')
        if not isinstance(raw_change_kind, str):
            diagnostics.append(
                f'world_fact_updates[{index}].change_kind 不是字符串，已丢弃'
            )
            continue
        change_kind = raw_change_kind.strip().lower()
        if change_kind not in WORLD_FACT_CHANGE_KINDS:
            diagnostics.append(
                f'world_fact_updates[{index}].change_kind 无效，已丢弃'
            )
            continue
        cleaned['change_kind'] = change_kind

        for field in _WORLD_FACT_UPDATE_OPTIONAL_TEXT_FIELDS:
            if field not in projected_item:
                continue
            raw_text = projected_item.get(field)
            if not isinstance(raw_text, str):
                diagnostics.append(
                    f'world_fact_updates[{index}].{field} 不是字符串，已忽略该字段'
                )
                continue
            text = raw_text.strip()
            if len(text) > MAX_WORLD_FACT_VALUE_CHARS:
                diagnostics.append(
                    f'world_fact_updates[{index}].{field} 超过 '
                    f'{MAX_WORLD_FACT_VALUE_CHARS} 字符，已截断'
                )
            cleaned[field] = text[:MAX_WORLD_FACT_VALUE_CHARS]

        for field, (limit, item_chars) in _WORLD_FACT_UPDATE_TEXT_LIST_FIELDS.items():
            if field not in projected_item:
                continue
            values, value_diagnostics = _normalize_optional_text_list(
                projected_item.get(field),
                path=f'world_fact_updates[{index}].{field}',
                limit=limit,
                item_chars=item_chars,
            )
            diagnostics.extend(value_diagnostics)
            if values is not None:
                cleaned[field] = values

        source_id = projected_item.get('source_world_story_thread_id')
        if change_kind != 'new':
            normalized_source_id, _ = normalize_ascii_integer(
                source_id,
                minimum=1,
                maximum=MAX_FACTION_PROFILE_DB_INTEGER,
            )
            if (
                type(normalized_source_id) is not int
                or not 1 <= normalized_source_id <= MAX_FACTION_PROFILE_DB_INTEGER
            ):
                diagnostics.append(
                    f'world_fact_updates[{index}] 的 {change_kind} 缺少有效 source_world_story_thread_id，已丢弃'
                )
                continue
            cleaned['source_world_story_thread_id'] = normalized_source_id
            normalized_revision, _ = normalize_ascii_integer(
                projected_item.get('source_revision'),
                minimum=1,
                maximum=MAX_FACTION_PROFILE_DB_INTEGER,
            )
            if (
                type(normalized_revision) is not int
                or not 1 <= normalized_revision <= MAX_FACTION_PROFILE_DB_INTEGER
            ):
                diagnostics.append(
                    f'world_fact_updates[{index}] 的 source_revision 不是精确正整数，已丢弃'
                )
                continue
            cleaned['source_revision'] = normalized_revision
        elif (
            'source_world_story_thread_id' in projected_item
            or 'source_revision' in projected_item
        ):
            diagnostics.append(
                f'world_fact_updates[{index}] 的 new 不得携带旧线身份，已丢弃'
            )
            continue
        if 'campaign_dispatch' in projected_item:
            from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import normalize_dispatch_report
            try:
                cleaned['campaign_dispatch'] = normalize_dispatch_report(projected_item['campaign_dispatch'])
            except ValueError as exc:
                diagnostics.append(f'world_fact_updates[{index}]: {exc}')
                # Preserve a legitimate world fact while the dedicated gate requires
                # an Observer repair of the missing campaign report.
        normalized.append(cleaned)
    return normalized, diagnostics


def validate_world_fact_updates_strict(value: object) -> tuple[list[dict] | None, list[str]]:
    """Validate an observer override without accepting any lossy sanitization."""
    if not isinstance(value, list):
        return None, ['world_fact_updates 必须是对象数组']
    normalized, diagnostics = normalize_world_fact_updates(value)
    if diagnostics:
        return None, diagnostics
    return normalized, []


def normalize_dm_handoff_note(value: object) -> tuple[str, list[str]]:
    """Keep the note optional; invalid auxiliary types are dropped fail-open."""
    if value is None:
        return '', []
    if not isinstance(value, str):
        return '', ['dm_handoff_note 必须是字符串，已丢弃非法值']
    return value.strip(), []


def normalize_cycle_reseed_transition(
    value: object,
) -> tuple[dict | None, list[str]]:
    """Project one reviewed closed-dungeon successor onto authoritative state fields."""
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ['cycle_reseed_transition 必须是对象']

    from plugins.adventurer_s1.services.s1_world_location_state_contract import (
        normalize_dungeon_explorability_change,
        normalize_location_state_changes,
    )

    raw = {
        str(key): item
        for key, item in value.items()
        if str(key) in _CYCLE_RESEED_TRANSITION_ALLOWED_FIELDS
    }
    location_changes, location_errors = normalize_location_state_changes(
        [raw.get('location_state_change')],
        max_changes=1,
    )
    explorability_change, explorability_errors = (
        normalize_dungeon_explorability_change(
            raw.get('dungeon_explorability_change'),
        )
    )
    errors = [*location_errors, *explorability_errors]
    if len(location_changes) != 1:
        if not location_errors:
            errors.append('cycle_reseed_transition 必须包含一个地点当前态变更')
    if explorability_change is None:
        if not explorability_errors:
            errors.append('cycle_reseed_transition 必须包含地牢可探索性变更')
    if errors:
        return None, errors
    return {
        'location_state_change': location_changes[0],
        'dungeon_explorability_change': explorability_change,
    }, []


def _normalize_world_story_threads(
    value: list,
    *,
    require_authoritative_fields: bool = True,
) -> tuple[list[dict], list[str], bool]:
    normalized: list[dict] = []
    diagnostics: list[str] = []
    contract_valid = True
    if len(value) > MAX_WORLD_FACT_CONTAINER_ITEMS:
        diagnostics.append(
            f'world_story_threads 超过建议上限 {MAX_WORLD_FACT_CONTAINER_ITEMS} 项，仍将逐项校验并全部处理'
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            diagnostics.append(f'world_story_threads[{index}] 不是对象，已丢弃')
            contract_valid = False
            continue
        title = item.get('title')
        summary = item.get('summary')
        if not isinstance(title, str) or not title.strip():
            diagnostics.append(f'world_story_threads[{index}].title 无效，已丢弃')
            contract_valid = False
            continue
        if not isinstance(summary, str) or not summary.strip():
            diagnostics.append(f'world_story_threads[{index}].summary 无效，已丢弃')
            contract_valid = False
            continue
        unknown = sorted(
            str(key)
            for key in item
            if str(key) not in _WORLD_STORY_THREAD_ALLOWED_FIELDS
            and str(key) not in _WORLD_STORY_THREAD_CONTEXT_ONLY_FIELDS
        )
        if unknown:
            diagnostics.append(
                f'world_story_threads[{index}] 含未知字段 {unknown}，已忽略未知字段'
            )
        cleaned: dict[str, Any] = {}
        for field in _WORLD_STORY_THREAD_TEXT_FIELDS:
            if field not in item:
                continue
            raw_text = item.get(field)
            if not isinstance(raw_text, str):
                diagnostics.append(
                    f'world_story_threads[{index}].{field} 不是字符串，已忽略该字段'
                )
                contract_valid = False
                continue
            text = raw_text.strip()
            if len(text) > MAX_WORLD_FACT_VALUE_CHARS:
                diagnostics.append(
                    f'world_story_threads[{index}].{field} 超过 '
                    f'{MAX_WORLD_FACT_VALUE_CHARS} 字符，已截断'
                )
                contract_valid = False
            cleaned[field] = text[:MAX_WORLD_FACT_VALUE_CHARS]
        cleaned['title'] = title.strip()[:MAX_WORLD_FACT_VALUE_CHARS]
        cleaned['summary'] = summary.strip()[:MAX_WORLD_FACT_VALUE_CHARS]

        raw_thread_type = (
            item.get('thread_type')
            if 'thread_type' in item
            else item.get('type') if 'type' in item else None
        )
        if raw_thread_type is not None:
            if (
                not isinstance(raw_thread_type, str)
                or raw_thread_type.strip() not in WORLD_STORY_THREAD_TYPES
            ):
                diagnostics.append(
                    f'world_story_threads[{index}].thread_type 无效，已丢弃该行'
                )
                contract_valid = False
                continue
            cleaned['thread_type'] = raw_thread_type.strip()
        if 'thread_type' in item and 'type' in item:
            raw_alias = item.get('type')
            if (
                not isinstance(raw_alias, str)
                or raw_alias.strip() != cleaned.get('thread_type')
            ):
                diagnostics.append(
                    f'world_story_threads[{index}] 的 thread_type/type 冲突，已丢弃该行'
                )
                contract_valid = False
                continue

        if 'status' in item:
            raw_status = item.get('status')
            if (
                not isinstance(raw_status, str)
                or raw_status.strip() not in WORLD_STORY_THREAD_STATUSES
            ):
                diagnostics.append(
                    f'world_story_threads[{index}].status 无效，已丢弃该行'
                )
                contract_valid = False
                continue
            cleaned['status'] = raw_status.strip()

        if 'change_kind' in item:
            raw_change_kind = item.get('change_kind')
            if (
                not isinstance(raw_change_kind, str)
                or raw_change_kind.strip().lower() not in WORLD_FACT_CHANGE_KINDS
            ):
                diagnostics.append(
                    f'world_story_threads[{index}].change_kind 无效，已丢弃该行'
                )
                contract_valid = False
                continue
            cleaned['change_kind'] = raw_change_kind.strip().lower()

        for field, (limit, item_chars) in _WORLD_STORY_THREAD_TEXT_LIST_FIELDS.items():
            raw_field = field
            if field == 'scope_keys' and field not in item and 'scope' in item:
                raw_field = 'scope'
            if raw_field not in item:
                continue
            values, value_diagnostics = _normalize_optional_text_list(
                item.get(raw_field),
                path=f'world_story_threads[{index}].{field}',
                limit=limit,
                item_chars=item_chars,
            )
            diagnostics.extend(value_diagnostics)
            if value_diagnostics:
                contract_valid = False
            if values is not None:
                cleaned[field] = values
            if field == 'scope_keys' and field in item and 'scope' in item:
                alias_values, alias_diagnostics = _normalize_optional_text_list(
                    item.get('scope'),
                    path=f'world_story_threads[{index}].scope',
                    limit=limit,
                    item_chars=item_chars,
                )
                diagnostics.extend(alias_diagnostics)
                if alias_diagnostics or alias_values != values:
                    diagnostics.append(
                        f'world_story_threads[{index}] 的 scope_keys/scope 冲突，已忽略 scope'
                    )
                    contract_valid = False
        invalid_identity_field = None
        for field, minimum in (
            ('id', 0),
            ('thread_id', 0),
            ('predecessor_id', 0),
            ('source_world_story_thread_id', 1),
            ('source_revision', 1),
            ('continuity_actor_id', 1),
        ):
            if field not in item:
                continue
            normalized_value, changed = normalize_ascii_integer(
                item.get(field),
                minimum=minimum,
                maximum=MAX_FACTION_PROFILE_DB_INTEGER,
            )
            if (
                type(normalized_value) is not int
                or not minimum <= normalized_value <= MAX_FACTION_PROFILE_DB_INTEGER
            ):
                invalid_identity_field = field
                break
            cleaned[field] = normalized_value
            if changed:
                diagnostics.append(
                    f'world_story_threads[{index}].{field} 数字字符串已无损转换为整数'
                )
        if invalid_identity_field is not None:
            diagnostics.append(
                f'world_story_threads[{index}].{invalid_identity_field} 不是精确整数，已丢弃'
            )
            contract_valid = False
            continue
        if 'weight' in item:
            normalized_weight, changed = normalize_ascii_integer(
                item.get('weight'),
                minimum=1,
                maximum=100,
            )
            if type(normalized_weight) is int and 1 <= normalized_weight <= 100:
                cleaned['weight'] = normalized_weight
                if changed:
                    diagnostics.append(
                        f'world_story_threads[{index}].weight 数字字符串已无损转换为整数'
                    )
            else:
                cleaned.pop('weight', None)
                diagnostics.append(
                    f'world_story_threads[{index}].weight 不是 1-100 精确整数，已忽略'
                )
                contract_valid = False
        if 'cycle_reseed' in item:
            normalized_cycle, changed = normalize_exact_boolean(
                item.get('cycle_reseed')
            )
            if type(normalized_cycle) is bool:
                cleaned['cycle_reseed'] = normalized_cycle
                if changed:
                    diagnostics.append(
                        f'world_story_threads[{index}].cycle_reseed 布尔字符串已无损转换'
                    )
            else:
                cleaned.pop('cycle_reseed', None)
                diagnostics.append(
                    f'world_story_threads[{index}].cycle_reseed 不是精确布尔值，已忽略'
                )
                contract_valid = False
        if require_authoritative_fields:
            missing_authoritative_fields = [
                field
                for field in (
                    'change_kind', 'thread_type', 'status', 'scope_keys', 'weight',
                )
                if field not in cleaned
            ]
            if missing_authoritative_fields:
                diagnostics.append(
                    f'world_story_threads[{index}] 缺少权威字段 '
                    f'{missing_authoritative_fields}，已丢弃该行'
                )
                contract_valid = False
                continue
            change_kind = cleaned['change_kind']
            if change_kind != 'new' and (
                type(cleaned.get('source_world_story_thread_id')) is not int
                or type(cleaned.get('source_revision')) is not int
            ):
                diagnostics.append(
                    f'world_story_threads[{index}] 的 {change_kind} 缺少准确 '
                    'source_world_story_thread_id/source_revision，已丢弃该行'
                )
                contract_valid = False
                continue
            requested_id = cleaned.get('id') or cleaned.get('thread_id')
            predecessor_id = cleaned.get('predecessor_id')
            source_id = cleaned.get('source_world_story_thread_id')
            if change_kind == 'new':
                if any(
                    field in cleaned
                    for field in (
                        'id', 'thread_id', 'predecessor_id',
                        'source_world_story_thread_id', 'source_revision',
                    )
                ):
                    diagnostics.append(
                        f'world_story_threads[{index}] 的 new 不得携带旧线身份，已丢弃该行'
                    )
                    contract_valid = False
                    continue
            elif change_kind in {'advance', 'close', 'resolve'}:
                if (
                    type(requested_id) is not int
                    or requested_id <= 0
                    or source_id != requested_id
                    or type(predecessor_id) is int
                ):
                    diagnostics.append(
                        f'world_story_threads[{index}] 的 {change_kind} 必须以同一 '
                        'id/source_world_story_thread_id 更新旧线，已丢弃该行'
                    )
                    contract_valid = False
                    continue
            elif change_kind == 'successor' and (
                type(predecessor_id) is not int
                or predecessor_id <= 0
                or source_id != predecessor_id
                or type(requested_id) is int
            ):
                diagnostics.append(
                    f'world_story_threads[{index}] 的 successor 必须以同一 '
                    'predecessor_id/source_world_story_thread_id 建立后继，已丢弃该行'
                )
                contract_valid = False
                continue
        normalized.append(cleaned)
    return normalized, diagnostics, contract_valid


def _normalize_world_actor_thread_links(
    value: object,
    *,
    path: str,
) -> tuple[list[dict], list[str], bool]:
    if not isinstance(value, list):
        return [], [f'{path} 必须是数组，已忽略该字段'], False
    diagnostics: list[str] = []
    normalized: list[dict] = []
    contract_valid = True
    for index, raw_link in enumerate(value[:4]):
        link_path = f'{path}[{index}]'
        if not isinstance(raw_link, dict):
            diagnostics.append(f'{link_path} 不是对象，已丢弃')
            contract_valid = False
            continue
        unknown = sorted(
            str(key) for key in raw_link
            if str(key) not in _WORLD_ACTOR_THREAD_LINK_ALLOWED_FIELDS
        )
        if unknown:
            diagnostics.append(f'{link_path} 含未知字段 {unknown}，已忽略未知字段')

        has_thread_id = 'thread_id' in raw_link
        thread_id, id_changed = normalize_ascii_integer(
            raw_link.get('thread_id'),
            minimum=1,
            maximum=MAX_FACTION_PROFILE_DB_INTEGER,
        )
        if has_thread_id and not (
            type(thread_id) is int
            and 1 <= thread_id <= MAX_FACTION_PROFILE_DB_INTEGER
        ):
            diagnostics.append(f'{link_path}.thread_id 不是精确正整数，已丢弃')
            contract_valid = False
            continue
        if id_changed:
            diagnostics.append(f'{link_path}.thread_id 数字字符串已无损转换为整数')
        raw_title = raw_link.get('thread_title')
        thread_title = raw_title.strip() if isinstance(raw_title, str) else ''
        if has_thread_id and thread_title:
            diagnostics.append(
                f'{link_path} 同时提供 thread_id/thread_title，已丢弃'
            )
            contract_valid = False
            continue
        if not has_thread_id and not thread_title:
            diagnostics.append(f'{link_path} 缺少 thread_id/thread_title，已丢弃')
            contract_valid = False
            continue

        if 'involvement' not in raw_link:
            diagnostics.append(f'{link_path}.involvement 缺失，已丢弃')
            contract_valid = False
            continue
        involvement = raw_link.get('involvement')
        if not isinstance(involvement, str) or involvement.strip() not in _WORLD_ACTOR_THREAD_INVOLVEMENTS:
            diagnostics.append(f'{link_path}.involvement 无效，已丢弃')
            contract_valid = False
            continue
        involvement = involvement.strip()

        if 'lease_required' not in raw_link:
            diagnostics.append(f'{link_path}.lease_required 缺失，已丢弃')
            contract_valid = False
            continue
        raw_lease_required = raw_link.get('lease_required')
        lease_required, lease_changed = normalize_exact_boolean(raw_lease_required)
        if type(lease_required) is not bool:
            diagnostics.append(f'{link_path}.lease_required 不是精确布尔值，已丢弃')
            contract_valid = False
            continue
        if lease_changed:
            diagnostics.append(f'{link_path}.lease_required 布尔字符串已无损转换')

        cleaned = {
            'involvement': involvement,
            'lease_required': lease_required,
        }
        if has_thread_id:
            cleaned['thread_id'] = thread_id
        else:
            cleaned['thread_title'] = thread_title
        raw_role = raw_link.get('narrative_role')
        if isinstance(raw_role, str) and raw_role.strip():
            cleaned['narrative_role'] = raw_role.strip()
        elif raw_role is not None:
            diagnostics.append(f'{link_path}.narrative_role 不是字符串，已忽略')
            contract_valid = False
        normalized.append(cleaned)
    if len(value) > 4:
        diagnostics.append(f'{path} 超过消费上限 4 项，已丢弃超出部分')
        contract_valid = False
    return normalized, diagnostics, contract_valid


def _normalize_world_actor_faction_relations(
    value: object,
    *,
    path: str,
) -> tuple[list[dict], list[str], bool]:
    if not isinstance(value, list):
        return [], [f'{path} 必须是对象数组，已忽略该字段'], False
    diagnostics: list[str] = []
    normalized: list[dict] = []
    contract_valid = True
    if len(value) > 6:
        diagnostics.append(f'{path} 超过消费上限 6 项，已丢弃超出部分')
        contract_valid = False
    for index, raw_relation in enumerate(value[:6]):
        relation_path = f'{path}[{index}]'
        if not isinstance(raw_relation, dict):
            diagnostics.append(f'{relation_path} 不是对象，已丢弃')
            contract_valid = False
            continue
        projected_relation = {
            field: raw_relation[field]
            for field in _WORLD_ACTOR_FACTION_RELATION_ALLOWED_FIELDS
            if field in raw_relation
        }
        cleaned: dict[str, str] = {}
        for field in _WORLD_ACTOR_FACTION_RELATION_TEXT_FIELDS:
            if field not in projected_relation:
                continue
            raw_text = projected_relation.get(field)
            if not isinstance(raw_text, str):
                diagnostics.append(
                    f'{relation_path}.{field} 不是字符串，已忽略该字段'
                )
                contract_valid = False
                continue
            cleaned[field] = raw_text.strip()
        if not cleaned.get('faction_key') or not cleaned.get('faction_name'):
            diagnostics.append(
                f'{relation_path} 缺少非空 faction_key/faction_name，已丢弃'
            )
            contract_valid = False
            continue
        normalized.append(cleaned)
    return normalized, diagnostics, contract_valid


def _normalize_world_actors(
    value: list,
    *,
    max_items: int = MAX_WORLD_FACT_CONTAINER_ITEMS,
    require_authoritative_fields: bool = True,
) -> tuple[list[dict], list[str], bool]:
    normalized: list[dict] = []
    diagnostics: list[str] = []
    contract_valid = True
    max_items = max(0, min(MAX_WORLD_FACT_CONTAINER_ITEMS, int(max_items)))
    if len(value) > max_items:
        diagnostics.append(
            f'world_actors 超过消费上限 {max_items} 项；为避免按模型顺序偏置，整组拒绝'
        )
        return [], diagnostics, False
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            diagnostics.append(f'world_actors[{index}] 不是对象，已丢弃')
            contract_valid = False
            continue
        raw_id = item.get('id')
        normalized_id, id_changed = normalize_ascii_integer(
            raw_id,
            minimum=1,
            maximum=MAX_FACTION_PROFILE_DB_INTEGER,
        )
        actor_id = normalized_id if type(normalized_id) is int else 0
        if raw_id is not None and not (
            1 <= actor_id <= MAX_FACTION_PROFILE_DB_INTEGER
        ):
            diagnostics.append(f'world_actors[{index}].id 无效，已丢弃')
            contract_valid = False
            continue
        if id_changed:
            diagnostics.append(
                f'world_actors[{index}].id 数字字符串已无损转换为整数'
            )
        cleaned: dict[str, Any] = {}
        for field in _WORLD_ACTOR_TEXT_FIELDS:
            if field not in item:
                continue
            raw_text = item.get(field)
            if not isinstance(raw_text, str):
                diagnostics.append(
                    f'world_actors[{index}].{field} 不是字符串，已忽略'
                )
                contract_valid = False
                continue
            cleaned[field] = raw_text.strip()[:MAX_WORLD_FACT_VALUE_CHARS]
            if len(raw_text.strip()) > MAX_WORLD_FACT_VALUE_CHARS:
                diagnostics.append(
                    f'world_actors[{index}].{field} 超过 '
                    f'{MAX_WORLD_FACT_VALUE_CHARS} 字符，已截断'
                )
                contract_valid = False
        name = cleaned.get('canonical_name') or cleaned.get('name')
        identity = cleaned.get('identity_summary')
        if actor_id <= 0 and (
            not isinstance(name, str) or not name.strip()
            or not isinstance(identity, str) or not identity.strip()
        ):
            diagnostics.append(
                f'world_actors[{index}] 新主体缺少 canonical_name/identity_summary，已丢弃'
            )
            contract_valid = False
            continue
        if actor_id > 0 and not any(
            key != 'id' and key in _WORLD_ACTOR_ALLOWED_FIELDS and value is not None
            for key, value in item.items()
        ):
            diagnostics.append(f'world_actors[{index}] 只有 id、没有可应用更新，已丢弃')
            contract_valid = False
            continue
        unknown = sorted(str(key) for key in item if str(key) not in _WORLD_ACTOR_ALLOWED_FIELDS)
        if unknown:
            diagnostics.append(f'world_actors[{index}] 含未知字段 {unknown}，已忽略未知字段')
        if actor_id > 0:
            cleaned['id'] = actor_id

        if 'actor_type' in item:
            raw_actor_type = item.get('actor_type')
            if (
                not isinstance(raw_actor_type, str)
                or raw_actor_type.strip() not in WORLD_ACTOR_TYPES
            ):
                diagnostics.append(
                    f'world_actors[{index}].actor_type 无效，已丢弃该行'
                )
                contract_valid = False
                continue
            cleaned['actor_type'] = raw_actor_type.strip()
        if 'status' in item:
            raw_status = item.get('status')
            if (
                not isinstance(raw_status, str)
                or raw_status.strip() not in WORLD_ACTOR_STATUSES
            ):
                diagnostics.append(
                    f'world_actors[{index}].status 无效，已丢弃该行'
                )
                contract_valid = False
                continue
            cleaned['status'] = raw_status.strip()
        if require_authoritative_fields:
            missing_authoritative_fields = [
                field for field in ('actor_type', 'status')
                if field not in cleaned
            ]
            if missing_authoritative_fields:
                diagnostics.append(
                    f'world_actors[{index}] 缺少权威字段 '
                    f'{missing_authoritative_fields}，已丢弃该行'
                )
                contract_valid = False
                continue

        for field, (limit, item_chars) in _WORLD_ACTOR_TEXT_LIST_FIELDS.items():
            if field not in item:
                continue
            raw_values = item.get(field)
            # 关键词超量时整字段不进入消费者，既有关键词也不会被清空。
            # 只保留该辅助字段的诊断，其它字段/类型/逐项错误仍使合同失败。
            keyword_overflow = (
                field == 'trigger_keywords'
                and isinstance(raw_values, list)
                and len(raw_values) > limit
            )
            keyword_overflow_only = False
            if keyword_overflow:
                # 复用同一元素/去重合同确认只超数量；结果仍整字段丢弃，
                # 这里临时放开数量仅为检查，不能把列表带入 cleaned。
                _, keyword_item_diagnostics = _normalize_optional_text_list(
                    raw_values,
                    path=f'world_actors[{index}].{field}',
                    limit=len(raw_values),
                    item_chars=item_chars,
                )
                diagnostics.extend(keyword_item_diagnostics)
                keyword_overflow_only = not keyword_item_diagnostics
            text_values, text_diagnostics = _normalize_optional_text_list(
                raw_values,
                path=f'world_actors[{index}].{field}',
                limit=limit,
                item_chars=item_chars,
            )
            diagnostics.extend(text_diagnostics)
            if text_diagnostics and not keyword_overflow_only:
                contract_valid = False
            if text_values is None:
                cleaned.pop(field, None)
            else:
                cleaned[field] = text_values
        if 'importance' in item:
            normalized_importance, importance_changed = normalize_ascii_integer(
                item.get('importance'),
                minimum=1,
                maximum=100,
            )
            if (
                type(normalized_importance) is int
                and 1 <= normalized_importance <= 100
            ):
                cleaned['importance'] = normalized_importance
                if importance_changed:
                    diagnostics.append(
                        f'world_actors[{index}].importance 数字字符串已无损转换为整数'
                    )
            else:
                cleaned.pop('importance', None)
                diagnostics.append(
                    f'world_actors[{index}].importance 不是 1-100 精确整数，已忽略'
                )
                contract_valid = False
        if 'capability_profile' in item:
            raw_profile = item.get('capability_profile')
            if isinstance(raw_profile, dict):
                cleaned['capability_profile'] = raw_profile
            else:
                diagnostics.append(
                    f'world_actors[{index}].capability_profile 不是对象，已忽略'
                )
                contract_valid = False
        if 'faction_relations' in item:
            relations, relation_diagnostics, relations_valid = (
                _normalize_world_actor_faction_relations(
                    item.get('faction_relations'),
                    path=f'world_actors[{index}].faction_relations',
                )
            )
            diagnostics.extend(relation_diagnostics)
            contract_valid = contract_valid and relations_valid
            if relations:
                cleaned['faction_relations'] = relations
        if 'thread_links' in item:
            thread_links, link_diagnostics, links_valid = _normalize_world_actor_thread_links(
                item.get('thread_links'),
                path=f'world_actors[{index}].thread_links',
            )
            diagnostics.extend(link_diagnostics)
            contract_valid = contract_valid and links_valid
            if thread_links:
                cleaned['thread_links'] = thread_links
        if actor_id > 0 and set(cleaned) == {'id'}:
            diagnostics.append(
                f'world_actors[{index}] 没有通过合同校验的可应用更新，已丢弃'
            )
            contract_valid = False
            continue
        normalized.append(cleaned)
    return normalized, diagnostics, contract_valid


def _normalize_optional_text_list(
    value: object,
    *,
    path: str,
    limit: int,
    item_chars: int,
) -> tuple[list[str] | None, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, list):
        return None, [f'{path} 必须是字符串数组，已忽略该字段']
    diagnostics: list[str] = []
    if len(value) > limit:
        return None, [
            f'{path} 超过硬上限 {limit} 项，为避免按模型顺序偏置，已忽略该字段'
        ]
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            diagnostics.append(f'{path}[{index}] 不是非空字符串，已丢弃')
            continue
        text = item.strip()
        if len(text) > item_chars:
            diagnostics.append(f'{path}[{index}] 超过 {item_chars} 字符，已丢弃')
            continue
        key = text.casefold()
        if key in seen:
            diagnostics.append(f'{path}[{index}] 重复，已丢弃')
            continue
        seen.add(key)
        result.append(text)
    return result, diagnostics


def _strict_positive_db_integer(value: object) -> int:
    normalized, _changed = normalize_ascii_integer(
        value,
        minimum=1,
        maximum=MAX_FACTION_PROFILE_DB_INTEGER,
    )
    if type(normalized) is not int:
        return 0
    return normalized if 0 < normalized <= MAX_FACTION_PROFILE_DB_INTEGER else 0


def normalize_faction_profile_updates(value: object) -> tuple[list[dict], list[str]]:
    """Normalize optional faction-dossier proposals without creating repair errors."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['faction_profile_updates 必须是对象数组，已忽略该辅助字段']

    diagnostics: list[str] = []
    if len(value) > MAX_FACTION_PROFILE_UPDATE_ITEMS:
        return [], [
            f'faction_profile_updates 超过硬上限 {MAX_FACTION_PROFILE_UPDATE_ITEMS} 项，'
            '为避免按模型顺序偏置，已忽略整个辅助字段'
        ]
    normalized: list[dict] = []
    for index, item in enumerate(value):
        path = f'faction_profile_updates[{index}]'
        if not isinstance(item, dict):
            diagnostics.append(f'{path} 不是对象，已丢弃')
            continue
        unknown = sorted(
            str(key) for key in item
            if str(key) not in _FACTION_PROFILE_ALLOWED_FIELDS
        )
        if unknown:
            diagnostics.append(f'{path} 含未知字段 {unknown}，已忽略未知字段')

        raw_id = item.get('id')
        profile_id = _strict_positive_db_integer(raw_id)
        if raw_id is not None and profile_id <= 0:
            diagnostics.append(f'{path}.id 无效，已丢弃')
            continue

        raw_revision = item.get('source_revision')
        source_revision = _strict_positive_db_integer(raw_revision)
        if profile_id > 0 and source_revision <= 0:
            diagnostics.append(f'{path} 更新已有档案但缺少有效 source_revision，已丢弃')
            continue

        cleaned: dict[str, Any] = {}
        if profile_id > 0:
            cleaned['id'] = profile_id
            cleaned['source_revision'] = source_revision
        for field in ('canonical_name', 'identity_summary', 'change_summary'):
            if field not in item:
                continue
            value_text = item.get(field)
            if not isinstance(value_text, str) or not value_text.strip():
                diagnostics.append(f'{path}.{field} 必须是非空字符串，已忽略该字段')
                continue
            field_limit = 160 if field == 'canonical_name' else MAX_FACTION_PROFILE_TEXT_CHARS
            if len(value_text.strip()) > field_limit:
                diagnostics.append(
                    f'{path}.{field} 超过 {field_limit} 字符，已忽略该字段'
                )
                continue
            cleaned[field] = value_text.strip()
        if not cleaned.get('change_summary'):
            diagnostics.append(f'{path}.change_summary 缺失或无效，已丢弃')
            continue

        status = item.get('status')
        if status is not None:
            status_text = str(status).strip().lower()
            if status_text not in FACTION_PROFILE_STATUSES:
                diagnostics.append(f'{path}.status 无效，已忽略该字段')
            else:
                cleaned['status'] = status_text
        importance = item.get('importance')
        if importance is not None:
            normalized_importance, importance_changed = normalize_ascii_integer(
                importance,
                minimum=1,
                maximum=100,
            )
            importance_value = (
                normalized_importance if type(normalized_importance) is int else 0
            )
            if not 1 <= importance_value <= 100:
                diagnostics.append(f'{path}.importance 必须是 1-100 整数，已忽略该字段')
            else:
                cleaned['importance'] = importance_value
                if importance_changed:
                    diagnostics.append(
                        f'{path}.importance 数字字符串已无损转换为整数'
                    )
        for field, limit, item_chars in (
            ('aliases', 8, 160),
            ('scope_keys', 8, 80),
            ('trigger_keywords', 10, 50),
        ):
            if field not in item:
                continue
            values, value_diagnostics = _normalize_optional_text_list(
                item.get(field),
                path=f'{path}.{field}',
                limit=limit,
                item_chars=item_chars,
            )
            diagnostics.extend(value_diagnostics)
            if values:
                cleaned[field] = values
            elif values == [] and profile_id > 0:
                diagnostics.append(
                    f'{path}.{field} 不支持通过空数组清空，已忽略该字段'
                )

        raw_facets = item.get('facet_updates', [])
        facet_updates: list[dict] = []
        if not isinstance(raw_facets, list):
            diagnostics.append(f'{path}.facet_updates 必须是数组，已忽略该字段')
            raw_facets = []
        elif len(raw_facets) > len(FACTION_PROFILE_FACETS):
            diagnostics.append(
                f'{path}.facet_updates 超过硬上限 {len(FACTION_PROFILE_FACETS)} 项，'
                '已忽略该嵌套数组'
            )
            raw_facets = []
        facet_counts: dict[str, int] = {}
        for raw_facet in raw_facets:
            if isinstance(raw_facet, dict):
                facet_key = str(raw_facet.get('facet') or '').strip().lower()
                if facet_key:
                    facet_counts[facet_key] = facet_counts.get(facet_key, 0) + 1
        reported_duplicate_facets: set[str] = set()
        for facet_index, raw_facet in enumerate(raw_facets):
            facet_path = f'{path}.facet_updates[{facet_index}]'
            if not isinstance(raw_facet, dict):
                diagnostics.append(f'{facet_path} 不是对象，已丢弃')
                continue
            facet_unknown = sorted(
                str(key) for key in raw_facet
                if str(key) not in _FACTION_PROFILE_FACET_ALLOWED_FIELDS
            )
            if facet_unknown:
                diagnostics.append(
                    f'{facet_path} 含未知字段 {facet_unknown}，已忽略未知字段'
                )
            facet = str(raw_facet.get('facet') or '').strip().lower()
            visibility = str(raw_facet.get('visibility') or '').strip().lower()
            summary = raw_facet.get('summary')
            if facet not in FACTION_PROFILE_FACETS:
                diagnostics.append(f'{facet_path}.facet 无效，已丢弃')
                continue
            if facet_counts.get(facet, 0) > 1:
                if facet not in reported_duplicate_facets:
                    diagnostics.append(
                        f'{path}.facet_updates 的 {facet} 重复，已丢弃全部冲突项'
                    )
                    reported_duplicate_facets.add(facet)
                continue
            if visibility not in FACTION_PROFILE_VISIBILITIES:
                diagnostics.append(f'{facet_path}.visibility 无效，已丢弃')
                continue
            if not isinstance(summary, str) or not summary.strip():
                diagnostics.append(f'{facet_path}.summary 无效，已丢弃')
                continue
            if len(summary.strip()) > MAX_FACTION_PROFILE_TEXT_CHARS:
                diagnostics.append(
                    f'{facet_path}.summary 超过 {MAX_FACTION_PROFILE_TEXT_CHARS} 字符，已丢弃'
                )
                continue
            facet_updates.append({
                'facet': facet,
                'visibility': visibility,
                'summary': summary.strip(),
            })
        if facet_updates:
            cleaned['facet_updates'] = facet_updates

        raw_links = item.get('thread_links', [])
        thread_links: list[dict] = []
        if not isinstance(raw_links, list):
            diagnostics.append(f'{path}.thread_links 必须是数组，已忽略该字段')
            raw_links = []
        elif len(raw_links) > 8:
            diagnostics.append(
                f'{path}.thread_links 超过硬上限 8 项，已忽略该嵌套数组'
            )
            raw_links = []
        link_counts: dict[tuple[int, str], int] = {}
        for raw_link in raw_links:
            if not isinstance(raw_link, dict):
                continue
            raw_link_id = raw_link.get('thread_id')
            link_id = _strict_positive_db_integer(raw_link_id)
            link_title = (
                raw_link.get('thread_title').strip()
                if isinstance(raw_link.get('thread_title'), str)
                else ''
            )
            link_key = (link_id, link_title.casefold())
            if link_id > 0 or link_title:
                link_counts[link_key] = link_counts.get(link_key, 0) + 1
        reported_duplicate_links: set[tuple[int, str]] = set()
        for link_index, raw_link in enumerate(raw_links):
            link_path = f'{path}.thread_links[{link_index}]'
            if not isinstance(raw_link, dict):
                diagnostics.append(f'{link_path} 不是对象，已丢弃')
                continue
            link_unknown = sorted(
                str(key) for key in raw_link
                if str(key) not in _FACTION_PROFILE_THREAD_LINK_ALLOWED_FIELDS
            )
            if link_unknown:
                diagnostics.append(
                    f'{link_path} 含未知字段 {link_unknown}，已忽略未知字段'
                )
            raw_thread_id = raw_link.get('thread_id')
            thread_id = _strict_positive_db_integer(raw_thread_id)
            thread_title = (
                raw_link.get('thread_title').strip()
                if isinstance(raw_link.get('thread_title'), str)
                else ''
            )
            if len(thread_title) > 120:
                diagnostics.append(f'{link_path}.thread_title 超过 120 字符，已丢弃')
                continue
            if thread_id > 0 and thread_title:
                diagnostics.append(
                    f'{link_path} 不得同时提供 thread_id 与 thread_title，已丢弃'
                )
                continue
            if thread_id <= 0 and not thread_title:
                diagnostics.append(f'{link_path} 缺少有效 thread_id/thread_title，已丢弃')
                continue
            relation_role = str(
                raw_link.get('relation_role') or 'involved'
            ).strip().lower()
            if relation_role not in FACTION_PROFILE_THREAD_ROLES:
                diagnostics.append(f'{link_path}.relation_role 无效，已丢弃')
                continue
            link_key = (thread_id, thread_title.casefold())
            if link_counts.get(link_key, 0) > 1:
                if link_key not in reported_duplicate_links:
                    diagnostics.append(
                        f'{path}.thread_links 含重复目标，已丢弃全部冲突项'
                    )
                    reported_duplicate_links.add(link_key)
                continue
            normalized_link = {'relation_role': relation_role}
            if thread_id > 0:
                normalized_link['thread_id'] = thread_id
            else:
                normalized_link['thread_title'] = thread_title
            thread_links.append(normalized_link)
        if thread_links:
            cleaned['thread_links'] = thread_links

        if profile_id <= 0:
            if not cleaned.get('canonical_name') or not cleaned.get('identity_summary'):
                diagnostics.append(
                    f'{path} 新档案缺少 canonical_name/identity_summary，已丢弃'
                )
                continue
            if not thread_links:
                diagnostics.append(f'{path} 新档案缺少精确 faction 世界线关联，已丢弃')
                continue
        elif not any(
            key not in {'id', 'source_revision', 'canonical_name', 'change_summary'}
            for key in cleaned
        ):
            diagnostics.append(f'{path} 没有可应用档案变化，已丢弃')
            continue
        normalized.append(cleaned)

    target_counts: dict[tuple[str, object], int] = {}
    for row in normalized:
        target = (
            ('id', int(row['id']))
            if int(row.get('id') or 0) > 0
            else ('name', str(row.get('canonical_name') or '').casefold())
        )
        target_counts[target] = target_counts.get(target, 0) + 1
    duplicate_targets = {
        target for target, count in target_counts.items() if count > 1
    }
    if duplicate_targets:
        diagnostics.append(
            'faction_profile_updates 含重复档案目标，已丢弃全部冲突项'
        )
        normalized = [
            row for row in normalized
            if (
                ('id', int(row['id']))
                if int(row.get('id') or 0) > 0
                else ('name', str(row.get('canonical_name') or '').casefold())
            ) not in duplicate_targets
        ]
    return normalized, diagnostics


def normalize_maintenance_result_with_status(
    value: object,
    *,
    cycle_reseed_only: bool = False,
    max_world_actor_updates: int = DEFAULT_WORLD_ACTOR_MAX_AI_UPDATES,
    require_core_fields: bool = True,
    require_authoritative_fields: bool = True,
    location_state_snapshot: dict | None = None,
    allow_location_evolution: bool = True,
) -> tuple[dict | None, list[str], list[str], bool]:
    """Return normalized output plus its real consumer-contract status.

    Structural errors may be sent to a format-only repair call. Invalid rows are
    auxiliary and are dropped locally so one bad row cannot discard valid siblings.
    """
    if not isinstance(value, dict):
        return None, ['顶层必须是 JSON 对象'], [], False

    structural_errors: list[str] = []
    diagnostics: list[str] = []
    required_core_fields = {
        'dm_handoff_note', 'world_story_threads', 'world_actors',
    }
    if not cycle_reseed_only:
        required_core_fields.add('ecology_continuity_debt_resolutions')
    if require_core_fields:
        missing_core_fields = sorted(required_core_fields.difference(value))
        if missing_core_fields:
            structural_errors.append(
                f'缺少核心顶层字段: {missing_core_fields}'
            )
    note_value = value.get('dm_handoff_note', '')
    if not isinstance(note_value, str):
        structural_errors.append('dm_handoff_note 必须是字符串')
    threads_value = _normalize_singleton_record_container(
        value.get('world_story_threads', []),
        field='world_story_threads',
        identity_fields=frozenset({'id', 'thread_id', 'title', 'summary'}),
        diagnostics=diagnostics,
    )
    if not isinstance(threads_value, list):
        structural_errors.append('world_story_threads 必须是数组')
    actors_value = _normalize_singleton_record_container(
        value.get('world_actors', []),
        field='world_actors',
        identity_fields=frozenset({
            'id', 'canonical_name', 'name', 'identity_summary',
        }),
        diagnostics=diagnostics,
    )
    if not isinstance(actors_value, list):
        structural_errors.append('world_actors 必须是数组')
    ecology_debt_resolutions = _normalize_singleton_record_container(
        value.get('ecology_continuity_debt_resolutions', []),
        field='ecology_continuity_debt_resolutions',
        identity_fields=frozenset({'expedition_log_id'}),
        diagnostics=diagnostics,
    )
    if not isinstance(ecology_debt_resolutions, list):
        structural_errors.append(
            'ecology_continuity_debt_resolutions 必须是数组'
        )
    if structural_errors:
        return None, structural_errors, [], False

    unknown = sorted(str(key) for key in value if str(key) not in _MAINTENANCE_ALLOWED_FIELDS)
    if unknown:
        diagnostics.append(f'维护结果含未知顶层字段 {unknown}，已忽略')
    threads, thread_diagnostics, threads_valid = _normalize_world_story_threads(
        threads_value,
        require_authoritative_fields=require_authoritative_fields,
    )
    actors, actor_diagnostics, actors_valid = _normalize_world_actors(
        actors_value,
        max_items=max_world_actor_updates,
        require_authoritative_fields=require_authoritative_fields,
    )
    faction_profiles, faction_profile_diagnostics = (
        normalize_faction_profile_updates(
            _normalize_singleton_record_container(
                value.get('faction_profile_updates', []),
                field='faction_profile_updates',
                identity_fields=frozenset({
                    'id', 'canonical_name', 'identity_summary', 'change_summary',
                }),
                diagnostics=diagnostics,
            ),
        )
    )
    transition, transition_diagnostics = normalize_cycle_reseed_transition(
        value.get('cycle_reseed_transition'),
    )
    diagnostics.extend(thread_diagnostics)
    diagnostics.extend(actor_diagnostics)
    diagnostics.extend(faction_profile_diagnostics)
    diagnostics.extend(transition_diagnostics)
    normalized_result = {
        'dm_handoff_note': note_value.strip(),
        'world_story_threads': threads,
        'world_actors': actors,
    }
    if 'ecology_continuity_debt_resolutions' in value:
        normalized_result['ecology_continuity_debt_resolutions'] = (
            ecology_debt_resolutions
        )
    if 'faction_profile_updates' in value:
        normalized_result['faction_profile_updates'] = faction_profiles
    if transition is not None:
        normalized_result['cycle_reseed_transition'] = transition
    # A closed server-owned capability has no consumed companion fields. Do not
    # validate or later restore an extra proposal that this batch cannot apply.
    if not cycle_reseed_only and allow_location_evolution and 'location_state_changes' in value:
        try:
            normalized_result['location_state_changes'] = normalize_maintenance_location_changes(
                value['location_state_changes'], location_state_snapshot,
            )
        except ValueError as exc:
            return None, [], [*diagnostics, str(exc)], False
    return normalized_result, [], diagnostics, bool(threads_valid and actors_valid)


def normalize_maintenance_result(
    value: object,
) -> tuple[dict | None, list[str], list[str]]:
    """Compatibility wrapper for callers that do not own token success metrics."""
    normalized, structural_errors, diagnostics, _contract_valid = (
        normalize_maintenance_result_with_status(
            value,
            max_world_actor_updates=MAX_WORLD_FACT_CONTAINER_ITEMS,
            require_core_fields=False,
            require_authoritative_fields=False,
        )
    )
    return normalized, structural_errors, diagnostics
