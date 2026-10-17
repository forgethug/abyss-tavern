"""S1 探险战斗存在要求的冻结、投影与旧主目标模式兼容。

新日志只随机 ``required`` / ``natural`` 两种真实协议状态；它们控制本场是否
必须发生有因果结果的战斗，不再把故事目标限制成若干封闭类型。正式委托和
隐藏 Boss 等结构化上层权威存在时只冻结 ``authority_bound``，不追加独立
战斗要求。

最终 AI 投影还接受服务端当前特殊事件类别：红黑本固定 required，高于
随机偏好与历史主目标模式；不改变正式委托的目标，也不修改原冻结快照。

已经冻结的 v1 ``objective_mode`` 快照仍在同一持久化容器中只读兼容，避免
重试或 worker 恢复改写存量探险。新输出 AI 不接收 v1 模式全集。
"""

from __future__ import annotations

import math
import random
from typing import Iterable, Mapping


# 沿用既有内部容器，避免为同一“发车冻结生成偏好”建立平行状态机。字段只在
# 管理员日志与后台大纲中存在，进入 AI 前还会再次白名单投影。
OBJECTIVE_MODE_OUTLINE_FIELD = 'objective_mode'
OBJECTIVE_MODE_SNAPSHOT_LOG_FIELD = '_objective_mode_snapshot'

OBJECTIVE_MODE_SCHEMA_VERSION = 1
COMBAT_PRESENCE_SCHEMA_VERSION = 2

# ``rotated`` 只用于兼容已经冻结在存量日志中的 v1 快照。
OBJECTIVE_MODE_SELECTION_WEIGHTED_RANDOM = 'weighted_random'
OBJECTIVE_MODE_SELECTION_ROTATED = 'rotated'
OBJECTIVE_MODE_SELECTION_AUTHORITY_BOUND = 'authority_bound'

COMBAT_PRESENCE_REQUIRED = 'required'
COMBAT_PRESENCE_NATURAL = 'natural'
COMBAT_PRESENCE_VALUES = (
    COMBAT_PRESENCE_REQUIRED,
    COMBAT_PRESENCE_NATURAL,
)

# 只用于读取已经冻结的 v1 日志；新日志不会再从这些故事含义中选择。
LEGACY_OBJECTIVE_MODES = (
    'confront',
    'investigate',
    'protect',
    'traverse',
    'transform',
)

OBJECTIVE_MODE_AUTHORITY_SOURCES = (
    'formal_commission',
    'hidden_boss',
)


def normalize_objective_mode_authority_sources(
    values: Iterable[object],
) -> list[str]:
    """按稳定优先级白名单化目标硬权威来源。"""
    supplied = {
        str(value).strip()
        for value in (values or ())
        if isinstance(value, str) and value.strip()
    }
    return [
        source for source in OBJECTIVE_MODE_AUTHORITY_SOURCES
        if source in supplied
    ]


def build_objective_mode_authority_sources(
    *,
    commission_context: object = None,
    hidden_boss_context: object = None,
) -> list[str]:
    """只从结构化输入判断是否已有上层目标，不读取或猜测剧情文本。"""
    sources: list[str] = []
    if isinstance(commission_context, (list, tuple)) and any(
        isinstance(item, Mapping) and item for item in commission_context
    ):
        sources.append('formal_commission')
    if isinstance(hidden_boss_context, Mapping) and hidden_boss_context:
        sources.append('hidden_boss')
    return normalize_objective_mode_authority_sources(sources)


def _project_snapshot_identity(
    value: Mapping[str, object],
    *,
    expected_target_dungeon: str | None,
    expected_expedition_log_id: int | None,
) -> tuple[str, int | None] | None:
    target_dungeon = value.get('target_dungeon')
    if not isinstance(target_dungeon, str) or not target_dungeon.strip():
        return None
    target_dungeon = target_dungeon.strip()
    if (
        expected_target_dungeon is not None
        and target_dungeon != str(expected_target_dungeon or '').strip()
    ):
        return None

    expedition_log_id = value.get('expedition_log_id')
    if expedition_log_id is not None and (
        type(expedition_log_id) is not int or expedition_log_id <= 0
    ):
        return None
    if (
        expected_expedition_log_id is not None
        and expedition_log_id != int(expected_expedition_log_id)
    ):
        return None
    return target_dungeon, expedition_log_id


def _project_authority_sources(value: Mapping[str, object]) -> list[str] | None:
    raw_authority_sources = value.get('authority_sources')
    if not isinstance(raw_authority_sources, list):
        return None
    authority_sources = normalize_objective_mode_authority_sources(
        raw_authority_sources,
    )
    if authority_sources != raw_authority_sources:
        return None
    return authority_sources


def _build_legacy_snapshot(
    *,
    target_dungeon: str,
    expedition_log_id: int | None,
    selection_kind: str,
    selected_mode: str | None,
    authority_sources: Iterable[object],
) -> dict:
    return {
        'schema_version': OBJECTIVE_MODE_SCHEMA_VERSION,
        'expedition_log_id': expedition_log_id,
        'target_dungeon': target_dungeon,
        'selection_kind': selection_kind,
        'selected_mode': selected_mode,
        'authority_sources': list(authority_sources),
    }


def _build_combat_presence_snapshot(
    *,
    target_dungeon: str,
    expedition_log_id: int | None,
    selection_kind: str,
    combat_presence: str | None,
    authority_sources: Iterable[object],
) -> dict:
    return {
        'schema_version': COMBAT_PRESENCE_SCHEMA_VERSION,
        'expedition_log_id': expedition_log_id,
        'target_dungeon': target_dungeon,
        'selection_kind': selection_kind,
        'combat_presence': combat_presence,
        'authority_sources': list(authority_sources),
    }


def project_objective_mode_snapshot(
    value,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> dict | None:
    """白名单投影 v2 战斗要求或已冻结的 v1 主目标模式。"""
    if not isinstance(value, Mapping):
        return None
    schema_version = value.get('schema_version')
    if type(schema_version) is not int or schema_version not in {
        OBJECTIVE_MODE_SCHEMA_VERSION,
        COMBAT_PRESENCE_SCHEMA_VERSION,
    }:
        return None

    identity = _project_snapshot_identity(
        value,
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    authority_sources = _project_authority_sources(value)
    if identity is None or authority_sources is None:
        return None
    target_dungeon, expedition_log_id = identity
    selection_kind = value.get('selection_kind')

    if schema_version == OBJECTIVE_MODE_SCHEMA_VERSION:
        selected_mode = value.get('selected_mode')
        if selection_kind in (
            OBJECTIVE_MODE_SELECTION_WEIGHTED_RANDOM,
            OBJECTIVE_MODE_SELECTION_ROTATED,
        ):
            if selected_mode not in LEGACY_OBJECTIVE_MODES or authority_sources:
                return None
        elif selection_kind == OBJECTIVE_MODE_SELECTION_AUTHORITY_BOUND:
            if selected_mode is not None or not authority_sources:
                return None
        else:
            return None
        return _build_legacy_snapshot(
            target_dungeon=target_dungeon,
            expedition_log_id=expedition_log_id,
            selection_kind=selection_kind,
            selected_mode=selected_mode,
            authority_sources=authority_sources,
        )

    combat_presence = value.get('combat_presence')
    if selection_kind == OBJECTIVE_MODE_SELECTION_WEIGHTED_RANDOM:
        if combat_presence not in COMBAT_PRESENCE_VALUES or authority_sources:
            return None
    elif selection_kind == OBJECTIVE_MODE_SELECTION_AUTHORITY_BOUND:
        if combat_presence is not None or not authority_sources:
            return None
    else:
        return None
    return _build_combat_presence_snapshot(
        target_dungeon=target_dungeon,
        expedition_log_id=expedition_log_id,
        selection_kind=selection_kind,
        combat_presence=combat_presence,
        authority_sources=authority_sources,
    )


def project_objective_mode_for_ai(
    value,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
    special_event_type: str | None = None,
) -> dict | None:
    """模式硬要求先于随机偏好；保留原快照，不追改已完成日志。"""
    from plugins.adventurer_s1.services.s1_ai_prompt_shared import (
        S1_RED_SPECIAL_EVENT_KINDS,
    )
    if special_event_type in S1_RED_SPECIAL_EVENT_KINDS | {'unbeatable_horror'}:
        return {'combat_presence': COMBAT_PRESENCE_REQUIRED}
    snapshot = project_objective_mode_snapshot(
        value,
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if not snapshot:
        return None
    if snapshot['schema_version'] == COMBAT_PRESENCE_SCHEMA_VERSION:
        if snapshot['selection_kind'] == OBJECTIVE_MODE_SELECTION_AUTHORITY_BOUND:
            return None
        return {
            'combat_presence': snapshot['combat_presence'],
        }

    selection_kind = snapshot['selection_kind']
    if selection_kind == OBJECTIVE_MODE_SELECTION_ROTATED:
        selection_kind = OBJECTIVE_MODE_SELECTION_WEIGHTED_RANDOM
    return {
        'selection_kind': selection_kind,
        'selected_mode': snapshot['selected_mode'],
    }


def normalize_combat_presence_weights(
    value: Mapping[str, object],
) -> tuple[float, ...]:
    """按战斗存在状态稳定顺序校验并投影配置权重。"""
    if not isinstance(value, Mapping):
        raise TypeError('战斗存在权重必须是对象')
    supplied_values = set(value)
    expected_values = set(COMBAT_PRESENCE_VALUES)
    if supplied_values != expected_values:
        raise ValueError('战斗存在权重必须完整覆盖协议状态全集')

    normalized: list[float] = []
    for presence in COMBAT_PRESENCE_VALUES:
        raw_weight = value.get(presence)
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise TypeError(f'战斗存在权重必须是数字: {presence}')
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f'战斗存在权重必须是有限正数: {presence}')
        normalized.append(weight)
    return tuple(normalized)


def select_combat_presence_snapshot(
    target_dungeon: str,
    *,
    expedition_log_id: int | None,
    authority_sources: Iterable[object] = (),
    presence_weights: Mapping[str, object],
) -> dict:
    """为一次无上层目标权威的探险独立选择战斗存在要求。"""
    dungeon = str(target_dungeon or '').strip()
    if not dungeon:
        raise ValueError('目标地牢不能为空')
    if expedition_log_id is not None and (
        type(expedition_log_id) is not int or expedition_log_id <= 0
    ):
        raise ValueError('探险日志 ID 必须是正整数或 None')

    normalized_authority_sources = normalize_objective_mode_authority_sources(
        authority_sources,
    )
    if normalized_authority_sources:
        return _build_combat_presence_snapshot(
            target_dungeon=dungeon,
            expedition_log_id=expedition_log_id,
            selection_kind=OBJECTIVE_MODE_SELECTION_AUTHORITY_BOUND,
            combat_presence=None,
            authority_sources=normalized_authority_sources,
        )

    normalized_weights = normalize_combat_presence_weights(presence_weights)
    combat_presence = random.choices(
        COMBAT_PRESENCE_VALUES,
        weights=normalized_weights,
        k=1,
    )[0]
    return _build_combat_presence_snapshot(
        target_dungeon=dungeon,
        expedition_log_id=expedition_log_id,
        selection_kind=OBJECTIVE_MODE_SELECTION_WEIGHTED_RANDOM,
        combat_presence=combat_presence,
        authority_sources=[],
    )
