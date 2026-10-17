"""深渊酒馆 S1 任务栏服务。

通用任务框架：任务模板定义在 JSON 文件中，添加新任务只需编辑 JSON。
服务负责：加载模板、接取检查、倒计时/探险完成、完成结算。

任务模板字段：
  - id: 唯一标识
  - name / description / icon / category: 展示信息
  - completion_type: timer（默认，倒计时）| expedition（探险通关）| building_help（累计帮工时长）| gathering（累计采集区时长）| work（累计打工区完成次数）| contribution（接取后累计获得贡献值）| treasure_hunt（接取后帮工/采集中随机掉落）| hidden_boss（随探险自动结算）
  - required_contribution: completion_type=contribution 时所需累计获得贡献值
  - daily_reset: true 时每人每天限完成 1 次（按 completed_at 北京时间日界重置，无需每日换任务 ID）
  - required_sessions: completion_type=work 时所需完成次数（默认 1）
  - required_dungeon: 指定副本名（可选；省略表示任意副本）
  - required_dungeon_min_level / required_dungeon_max_level: 指定推荐等级区间（如 3-5 档副本）
  - no_damage: completion_type=expedition 时，true 表示须无伤通关
  - repeatable: true 时同一冒险者可多次完成（仍受 max_completions 全服上限约束）
  - max_completions: 全服可完成次数上限（接取即占名额，放弃释放；完成后名额不恢复）
  - requirements: {min_level, required_stat, required_stat_min, stamina_cost}
  - duration_minutes: 任务时长（timer 类型）
  - rewards: {gold, exp, materials, stat, items:[{name, quantity?}]}
  - narrative: 完成后的叙事文本
"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    S1PlayerVisibleError, player_operation_failure,
)
import json
import logging
import os
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional, Tuple

from flask import current_app
from sqlalchemy import and_, case, func, or_
from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.services.s1_reward_item_preview_service import (
    collect_reward_item_names,
    enrich_reward_item_entries,
    load_item_preview_map as _load_item_preview_map,
    reward_item_entry_name as _reward_item_entry_name,
)
from utils.cache_manager import CacheManager

logger = logging.getLogger(__name__)
_HIDDEN_BOSS_TASK_ID = 'hidden_boss_hunt_task_v2'
_DAILY_SOLO_RANDOM_TASK_ID = 'daily_solo_random_wheel'
_DAILY_CONTRIBUTION_TASK_ID = 'daily_contribution_wheel'
_DYNAMIC_MAP_BOUNTY_SOURCE_ID = 'dynamic_map_bounty'
_DAILY_RESET_SOURCE_IDS = frozenset({
    _DAILY_SOLO_RANDOM_TASK_ID,
    _DAILY_CONTRIBUTION_TASK_ID,
})
_CONTRIBUTION_TASK_EXCLUDED_SOURCES = frozenset({'contribution_reconcile'})
_LEGACY_HIDDEN_BOSS_TASK_IDS = ('hidden_boss_hunt',)
_DEFAULT_DISABLED_TASK_IDS = (
    _HIDDEN_BOSS_TASK_ID,
    _DYNAMIC_MAP_BOUNTY_SOURCE_ID,
    *_LEGACY_HIDDEN_BOSS_TASK_IDS,
    'goblin_mine_treasure_hunt',
)


def _invalidate_owner_home_badges(adv) -> None:
    owner_id = getattr(adv, 'owner_id', None)
    if not owner_id:
        return
    try:
        from plugins.adventurer_s1.services.adventurer_s1_home_service import (
            invalidate_home_badges_cache,
        )
        invalidate_home_badges_cache(owner_id)
        invalidate_summarize_acceptable_tasks_cache(adv.id)
    except Exception:
        pass


# 任务模板缓存
_templates_cache: Optional[list] = None
_templates_map: Optional[dict] = None

# TaskOverride 缓存（TTL 10 秒，避免每次请求全表扫描）
_overrides_cache: Optional[dict] = None
_overrides_cache_at: float = 0
_OVERRIDES_CACHE_TTL = 10.0


def _json_array_is_empty_or_null(column):
    """兼容 SQLite/PostgreSQL，且不对 JSON 标量调用 array_length。"""
    json_type = (
        func.json_type(column)
        if db.engine.dialect.name == 'sqlite'
        else func.json_typeof(column)
    )
    array_length = func.json_array_length(column)
    return or_(
        column.is_(None),
        json_type == 'null',
        case((json_type == 'array', array_length), else_=None) == 0,
    )

# 隐藏 Boss 全服进度（角标/任务列表共用，60s）
_hidden_boss_progress_cache: Optional[dict] = None
_hidden_boss_progress_at: float = 0
_HIDDEN_BOSS_PROGRESS_CACHE_TTL = 60.0

_SUMMARIZE_ACCEPTABLE_CACHE_TTL_SECONDS = 60
_summarize_acceptable_cache = CacheManager(
    ttl_seconds=_SUMMARIZE_ACCEPTABLE_CACHE_TTL_SECONDS, max_size=1000,
)


def invalidate_summarize_acceptable_tasks_cache(adventurer_id: Optional[int] = None) -> None:
    """角标 summarize 缓存失效；接取/完成/管理员改任务后调用。"""
    if adventurer_id is None:
        _summarize_acceptable_cache.clear()
    else:
        _summarize_acceptable_cache.delete(adventurer_id)


def _cached_hidden_boss_progress() -> dict:
    """全服隐藏 Boss 进度快照，避免角标路径重复查库。"""
    import time
    global _hidden_boss_progress_cache, _hidden_boss_progress_at
    now = time.monotonic()
    if (
        _hidden_boss_progress_cache is not None
        and now - _hidden_boss_progress_at < _HIDDEN_BOSS_PROGRESS_CACHE_TTL
    ):
        return _hidden_boss_progress_cache
    progress = AdventurerS1TaskService.hidden_boss_progress()
    _hidden_boss_progress_cache = progress
    _hidden_boss_progress_at = now
    return progress


@dataclass(frozen=True)
class _TaskOverrideSnapshot:
    """Session-independent task override data safe for cross-request caching."""

    source_template_id: Optional[str]
    enabled: bool
    overrides_json: Optional[dict]


def _load_templates() -> Tuple[list, dict]:
    """加载任务模板 JSON，带缓存。"""
    global _templates_cache, _templates_map
    if _templates_cache is not None:
        return _templates_cache, _templates_map

    data_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        'data', 's1_task_templates.json',
    )
    try:
        with open(data_path, 'r', encoding='utf-8') as f:
            templates = json.load(f)
    except FileNotFoundError:
        logger.warning('任务模板文件不存在: %s', data_path)
        templates = []
    except json.JSONDecodeError as e:
        logger.error('任务模板 JSON 解析失败: %s', e)
        templates = []

    _templates_cache = templates
    _templates_map = {t['id']: t for t in templates}
    return _templates_cache, _templates_map


def reload_templates():
    """强制重新加载模板（用于热更新）。"""
    global _templates_cache, _templates_map
    _templates_cache = None
    _templates_map = None
    return _load_templates()


def _preload_all_overrides() -> dict:
    """一条 SQL 加载全部 TaskOverride 快照，带 10 秒 TTL 缓存。"""
    import time
    global _overrides_cache, _overrides_cache_at
    now = time.monotonic()
    if _overrides_cache is not None and now - _overrides_cache_at < _OVERRIDES_CACHE_TTL:
        return _overrides_cache
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride
    rows = db_manager.query(
        AdventurerS1TaskOverride.task_template_id,
        AdventurerS1TaskOverride.source_template_id,
        AdventurerS1TaskOverride.enabled,
        AdventurerS1TaskOverride.overrides_json,
    ).all()
    _overrides_cache = {
        row.task_template_id: _TaskOverrideSnapshot(
            source_template_id=row.source_template_id,
            enabled=bool(row.enabled),
            overrides_json=deepcopy(row.overrides_json) if row.overrides_json else None,
        )
        for row in rows
    }
    _overrides_cache_at = now
    return _overrides_cache


def _invalidate_overrides_cache():
    """清除 TaskOverride 缓存（admin 写操作后调用）。"""
    global _overrides_cache, _overrides_cache_at
    _overrides_cache = None
    _overrides_cache_at = 0
    invalidate_summarize_acceptable_tasks_cache()


def _load_task_instance_stats(
    adventurer_id: Optional[int] = None,
    task_template_ids: Optional[set[str]] = None,
) -> tuple[dict, set[str], set[str]]:
    """聚合任务占用统计，并按需读取当前冒险者已完成/今日已完成的每日任务。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskInstance

    completed_expr = func.sum(case(
        (AdventurerS1TaskInstance.status == 'completed', 1), else_=0,
    ))
    occupied_expr = func.sum(case(
        (AdventurerS1TaskInstance.status.in_(('accepted', 'completed')), 1), else_=0,
    ))
    stats_query = (
        db_manager.query(
            AdventurerS1TaskInstance.task_template_id,
            completed_expr.label('completed_count'),
            occupied_expr.label('occupied_count'),
        )
        .filter(AdventurerS1TaskInstance.status.in_(('accepted', 'completed')))
    )
    if task_template_ids is not None:
        stats_query = stats_query.filter(
            AdventurerS1TaskInstance.task_template_id.in_(task_template_ids),
        )
    rows = (
        stats_query.group_by(AdventurerS1TaskInstance.task_template_id).all()
        if task_template_ids is None or task_template_ids
        else []
    )
    stats = {
        row.task_template_id: {
            'completed': int(row.completed_count or 0),
            'occupied': int(row.occupied_count or 0),
        }
        for row in rows
    }

    completed_task_ids: set[str] = set()
    if adventurer_id:
        completed_rows = (
            db_manager.query(AdventurerS1TaskInstance.task_template_id)
            .filter(
                AdventurerS1TaskInstance.adventurer_id == adventurer_id,
                AdventurerS1TaskInstance.status == 'completed',
            )
            .distinct()
            .all()
        )
        completed_task_ids = {row[0] for row in completed_rows}

    completed_today_daily_reset_sources: set[str] = set()
    if adventurer_id:
        completed_today_daily_reset_sources = _load_completed_today_daily_reset_sources(adventurer_id)

    return stats, completed_task_ids, completed_today_daily_reset_sources


def _task_stat(stats_map: dict, task_template_id: str, field: str) -> int:
    """读取聚合统计；不存在的任务按 0 处理。"""
    return int(stats_map.get(task_template_id, {}).get(field, 0))


# 管理员可在 UI 直接编辑的模板字段（completion_type 等结构性字段仍走 JSON 文件）
ADMIN_EDITABLE_TEMPLATE_FIELDS = {
    'name', 'description', 'icon', 'duration_minutes', 'required_sessions',
    'required_contribution',
    'required_dungeon', 'required_dungeon_min_level', 'required_dungeon_max_level',
    'no_damage', 'required_random_solo_match', 'repeatable', 'max_completions', 'rewards', 'narrative',
    'requirements',
}


def _deep_merge_dict(base: dict, patch: dict) -> dict:
    merged = deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_task_override(task_template_id: str):
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride
    return AdventurerS1TaskOverride.query.session.get(AdventurerS1TaskOverride, task_template_id)


def _resolve_effective_template(base_template: dict, override_map: dict = None) -> tuple[dict, Any]:
    """合并 JSON 模板与数据库覆盖层（仅 JSON 根模板）。"""
    if override_map is not None:
        override = override_map.get(base_template['id'])
    else:
        override = _get_task_override(base_template['id'])
    if not override or not override.overrides_json:
        effective = deepcopy(base_template)
    else:
        effective = _deep_merge_dict(base_template, override.overrides_json)
    effective['id'] = base_template['id']
    return effective, override


def _resolve_effective_template_by_id(task_template_id: str, override_map: dict = None) -> tuple[Optional[dict], Any]:
    """按任务 ID 解析有效模板（JSON 根模板或派生新版本）。"""
    _, json_map = _load_templates()
    if override_map is not None:
        override = override_map.get(task_template_id)
    else:
        override = _get_task_override(task_template_id)

    if override and override.source_template_id:
        base = json_map.get(override.source_template_id)
        if not base:
            return None, override
        effective = deepcopy(base)
        if override.overrides_json:
            effective = _deep_merge_dict(effective, override.overrides_json)
        effective['id'] = task_template_id
        return effective, override

    base = json_map.get(task_template_id)
    if not base:
        return None, override
    return _resolve_effective_template(base, override_map=override_map)


def _runtime_source_id(task_template_id: str) -> Optional[str]:
    """返回 JSON 根模板 ID（派生任务回溯到 source）。"""
    override = _get_task_override(task_template_id)
    if override and override.source_template_id:
        return override.source_template_id
    _, json_map = _load_templates()
    if task_template_id in json_map:
        return task_template_id
    return None


def _next_edition_task_id(source_template_id: str) -> str:
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride

    prefix = f'{source_template_id}__r'
    rows = AdventurerS1TaskOverride.query.filter(
        AdventurerS1TaskOverride.task_template_id.like(f'{prefix}%'),
    ).all()
    max_run = 0
    for row in rows:
        suffix = row.task_template_id[len(prefix):]
        if suffix.isdigit():
            max_run = max(max_run, int(suffix))
    return f'{prefix}{max_run + 1}'


def _snapshot_editable_fields(template: dict) -> dict:
    snapshot = {}
    for key in ADMIN_EDITABLE_TEMPLATE_FIELDS:
        if key not in template:
            continue
        value = template[key]
        if isinstance(value, dict):
            snapshot[key] = deepcopy(value)
        else:
            snapshot[key] = value
    return snapshot


def _runtime_templates_map(include_disabled: bool = False, override_map: dict = None) -> dict:
    """玩家/管理员可见的运行时任务模板（JSON 根 + 派生新版本）。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride

    if override_map is None:
        override_map = _preload_all_overrides()

    json_list, _ = _load_templates()
    result: dict[str, dict] = {}

    for base in json_list:
        task_id = base['id']
        if not include_disabled and task_id in _LEGACY_HIDDEN_BOSS_TASK_IDS:
            continue
        if not include_disabled and not _is_task_enabled(task_id, override_map=override_map):
            continue
        effective, _ = _resolve_effective_template(base, override_map=override_map)
        result[task_id] = effective

    # 派生任务：从预加载 override_map 中筛选有 source_template_id 的
    for task_id, override in override_map.items():
        if not override.source_template_id:
            continue
        if (
            not include_disabled
            and (task_id in _LEGACY_HIDDEN_BOSS_TASK_IDS or override.source_template_id in _LEGACY_HIDDEN_BOSS_TASK_IDS)
        ):
            continue
        if not include_disabled and not override.enabled:
            continue
        _, json_map = _load_templates()
        base = json_map.get(override.source_template_id)
        if (
            base
            and _is_daily_reset_task(base)
            and task_id != override.source_template_id
        ):
            continue
        if (
            not include_disabled
            and override.source_template_id == _HIDDEN_BOSS_TASK_ID
            and not _is_task_enabled(override.source_template_id, override_map=override_map)
        ):
            continue
        effective, _ = _resolve_effective_template_by_id(task_id, override_map=override_map)
        if effective:
            result[task_id] = effective

    return result


def _task_instance_query(task_template_id: str, override=None):
    from plugins.adventurer_s1.models import AdventurerS1TaskInstance
    return AdventurerS1TaskInstance.query.filter_by(task_template_id=task_template_id)


def _is_task_enabled(task_template_id: str, override=None, override_map: dict = None) -> bool:
    if override is None:
        if override_map is not None:
            override = override_map.get(task_template_id)
        else:
            override = _get_task_override(task_template_id)
    if not override:
        # 限时活动默认关闭，需 GM 开启（见 seed_limited_task_defaults）
        if task_template_id in _DEFAULT_DISABLED_TASK_IDS:
            return False
        return True
    return bool(override.enabled)


def _completion_type(template: dict) -> str:
    return template.get('completion_type') or 'timer'


def _is_expedition_task(template: dict) -> bool:
    return _completion_type(template) == 'expedition'


def _is_building_help_task(template: dict) -> bool:
    return _completion_type(template) == 'building_help'


def _is_gathering_task(template: dict) -> bool:
    return _completion_type(template) == 'gathering'


def _is_work_task(template: dict) -> bool:
    return _completion_type(template) == 'work'


def _is_contribution_task(template: dict) -> bool:
    return _completion_type(template) == 'contribution'


def _required_contribution(template: dict) -> int:
    return max(1, int(template.get('required_contribution', 0) or 0))


def _is_daily_reset_task(template: dict) -> bool:
    return bool(template.get('daily_reset'))


def _daily_reset_source_id(task_template_id: str) -> Optional[str]:
    """每日重置任务的根模板 ID（含旧 __d/__r 派生 ID）。"""
    source = _runtime_source_id(task_template_id) or task_template_id
    if source in _DAILY_RESET_SOURCE_IDS:
        return source
    _, json_map = _load_templates()
    base = json_map.get(source)
    if base and _is_daily_reset_task(base):
        return source
    return None


def _beijing_today_start_utc() -> datetime:
    from utils.time_utils import get_beijing_today_start_utc
    return get_beijing_today_start_utc()


def _daily_reset_family_filter(source_id: str):
    """匹配根 ID 及历史 __d / __r 派生 ID。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskInstance

    return or_(
        AdventurerS1TaskInstance.task_template_id == source_id,
        AdventurerS1TaskInstance.task_template_id.like(f'{source_id}__d%'),
        AdventurerS1TaskInstance.task_template_id.like(f'{source_id}__r%'),
    )


def _load_completed_today_daily_reset_sources(adventurer_id: int) -> set[str]:
    """批量读取冒险者今日已完成的每日重置任务根 ID。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskInstance

    if not _DAILY_RESET_SOURCE_IDS:
        return set()
    today_start = _beijing_today_start_utc()
    family_filters = [_daily_reset_family_filter(sid) for sid in _DAILY_RESET_SOURCE_IDS]
    rows = (
        db_manager.query(AdventurerS1TaskInstance.task_template_id)
        .filter(
            AdventurerS1TaskInstance.adventurer_id == adventurer_id,
            AdventurerS1TaskInstance.status == 'completed',
            AdventurerS1TaskInstance.completed_at >= today_start,
            or_(*family_filters),
        )
        .distinct()
        .all()
    )
    result: set[str] = set()
    for row in rows:
        source_id = _daily_reset_source_id(row[0])
        if source_id:
            result.add(source_id)
    return result


def _adventurer_completed_today(source_id: str, adventurer_id: int) -> bool:
    """每日重置任务：今日（北京时间）是否已完成。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskInstance

    today_start = _beijing_today_start_utc()
    return db_manager.query(AdventurerS1TaskInstance.id).filter(
        AdventurerS1TaskInstance.adventurer_id == adventurer_id,
        AdventurerS1TaskInstance.status == 'completed',
        AdventurerS1TaskInstance.completed_at >= today_start,
        _daily_reset_family_filter(source_id),
    ).first() is not None


def _adventurer_accept_blocked(
    template: dict, task_template_id: str, adventurer_id: int,
) -> tuple[bool, Optional[str]]:
    """是否因已完成而不可再接取（含每日重置）。"""
    if _is_repeatable_task(template):
        return False, None
    source_id = _daily_reset_source_id(task_template_id)
    if source_id:
        if _adventurer_completed_today(source_id, adventurer_id):
            return True, '今日已完成该任务，不可重复领取'
        return False, None
    if AdventurerS1TaskService._adventurer_has_completed(task_template_id, adventurer_id):
        return True, '你已完成过该任务，不可重复领取'
    return False, None


def _is_treasure_hunt_task(template: dict) -> bool:
    return _completion_type(template) == 'treasure_hunt'


def _is_hidden_boss_task(template: dict) -> bool:
    return _completion_type(template) == 'hidden_boss'


def _requires_random_solo_match(template: dict) -> bool:
    return bool(template.get('required_random_solo_match'))


def _is_task_hidden_boss_victory_log(full_log: dict | None) -> bool:
    """Only task-random hidden Boss runs can complete task-board hidden Boss tasks."""
    if not isinstance(full_log, dict):
        return False
    from plugins.adventurer_s1.services.s1_hidden_boss_service import (
        HIDDEN_BOSS_COMPLETION_FIELD,
        LEGACY_HIDDEN_BOSS_COMPLETION_FIELD,
    )

    completed = int(
        full_log.get(HIDDEN_BOSS_COMPLETION_FIELD)
        or full_log.get(LEGACY_HIDDEN_BOSS_COMPLETION_FIELD)
        or 0
    ) == 1
    if not completed:
        return False
    ctx = full_log.get('hidden_boss_context')
    if not isinstance(ctx, dict):
        return False
    return (
        ctx.get('source') == 'hidden_boss_task'
        and ctx.get('trigger_mode') == 'task_random'
        and not ctx.get('clue_bureau_boss_id')
    )


def _completion_limited_unlimited_accept(template: dict) -> bool:
    """只按全服完成进度结束，不限制同时接取人数。"""
    return _is_treasure_hunt_task(template) or _is_hidden_boss_task(template)


def _work_required_sessions(template: dict) -> int:
    return max(1, int(template.get('required_sessions', 1) or 1))


def _is_repeatable_task(template: dict) -> bool:
    return bool(template.get('repeatable'))


def _required_help_seconds(template: dict) -> int:
    """building_help 类型所需累计帮工秒数（由 duration_minutes 换算）。"""
    return int(template.get('duration_minutes', 0) or 0) * 60


def _collect_reward_item_names_from_tasks(tasks: list[dict]) -> set[str]:
    names: set[str] = set()
    for task in tasks or []:
        names.update(collect_reward_item_names((task.get('rewards') or {}).get('items')))
    return names


def _requirements_for_client(requirements: dict | None) -> dict:
    requirements = requirements or {}
    return {
        key: requirements[key]
        for key in ('min_level', 'required_stat', 'required_stat_min', 'stamina_cost')
        if key in requirements
        and isinstance(requirements[key], (str, int, float, type(None)))
    }


def _enrich_rewards_for_display(
    rewards: dict | None,
    preview_by_name: dict[str, dict] | None = None,
) -> dict:
    """为奖励预览补充图鉴展示字段（列表/详情 API 专用，不影响结算逻辑）。"""
    rewards = rewards or {}
    out = {
        key: rewards[key] for key in ('gold', 'exp', 'drop_mode')
        if key in rewards and isinstance(rewards[key], (str, int, float, type(None)))
    }
    for key in ('materials', 'stat'):
        if isinstance(rewards.get(key), dict):
            out[key] = {
                name: amount for name, amount in rewards[key].items()
                if isinstance(amount, (int, float))
            }
    if isinstance(rewards.get('items'), list):
        out['items'] = enrich_reward_item_entries(rewards['items'], preview_by_name)
    return out


def _enrich_task_list_rewards(tasks: list[dict]) -> None:
    preview_map = _load_item_preview_map(_collect_reward_item_names_from_tasks(tasks))
    if not preview_map:
        return
    for task in tasks:
        if (task.get('rewards') or {}).get('items'):
            task['rewards'] = _enrich_rewards_for_display(task.get('rewards'), preview_map)


def _task_max_completions(template: dict) -> Optional[int]:
    """任务全服完成名额上限（兼容旧字段 max_acceptances）。"""
    raw = template.get('max_completions', template.get('max_acceptances'))
    if raw is None:
        return None
    return int(raw)


class AdventurerS1TaskService:
    """任务栏建筑的任务服务。"""

    STAT_NAMES = {'strength': '力量', 'agility': '敏捷', 'intelligence': '智力', 'luck': '运气'}

    @staticmethod
    def _template_for_client(template: dict | None) -> dict:
        if not template:
            return {}
        out = {
            key: template[key] for key in (
                'id', 'name', 'description', 'icon', 'category', 'duration_minutes',
                'narrative', 'completion_type', 'required_dungeon',
                'required_dungeon_min_level', 'required_dungeon_max_level',
                'required_random_solo_match', 'no_damage', 'required_contribution',
                'repeatable', 'max_completions', 'max_acceptances',
            )
            if key in template
            and isinstance(template[key], (str, int, float, bool, type(None)))
        }
        out['requirements'] = _requirements_for_client(template.get('requirements'))
        out['rewards'] = _enrich_rewards_for_display(template.get('rewards'))
        if _is_work_task(template):
            out['work_required_sessions'] = _work_required_sessions(template)
        return out

    @staticmethod
    def _sum_building_help_seconds(adventurer_id: int, since: datetime) -> int:
        """统计冒险者自 since 起在建造工地的累计在场秒数（可跨多次、多建筑）。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1,
            AdventurerS1BuildingHelper,
            AdventurerS1BuildingInstance,
        )

        now = _portable_utc_now()
        helpers = AdventurerS1BuildingHelper.query.filter(
            AdventurerS1BuildingHelper.adventurer_id == adventurer_id,
            AdventurerS1BuildingHelper.helped_at >= since,
        ).all()
        # 会话在接取前开始、接取后仍在场：只计 since 之后的部分
        overlap_helpers = AdventurerS1BuildingHelper.query.filter(
            AdventurerS1BuildingHelper.adventurer_id == adventurer_id,
            AdventurerS1BuildingHelper.helped_at < since,
            or_(
                AdventurerS1BuildingHelper.left_at.is_(None),
                AdventurerS1BuildingHelper.left_at > since,
            ),
        ).all()
        helpers = list(helpers) + list(overlap_helpers)

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        building_ids = {h.building_instance_id for h in helpers if h.building_instance_id}
        building_statuses = {}
        if building_ids:
            building_statuses = dict(
                AdventurerS1BuildingInstance.query.filter(
                    AdventurerS1BuildingInstance.id.in_(building_ids),
                ).with_entities(
                    AdventurerS1BuildingInstance.id,
                    AdventurerS1BuildingInstance.status,
                ).all()
            )

        total = 0
        seen_ids: set[int] = set()
        for h in helpers:
            if h.id in seen_ids:
                continue
            seen_ids.add(h.id)
            start = max(h.helped_at, since)
            end = h.left_at
            if end is None:
                is_still_helping_here = bool(
                    adv
                    and adv.status == 'building'
                    and adv.building_instance_id == h.building_instance_id
                    and building_statuses.get(h.building_instance_id) in ('building', 'upgrading')
                )
                if is_still_helping_here:
                    end = now
                elif adv and adv.building_assigned_at and adv.building_assigned_at > start:
                    # 历史残留记录最多计到下一次建筑行为开始，不能随训练/学习继续增长。
                    end = min(now, adv.building_assigned_at)
                else:
                    end = start
            if end <= start:
                continue
            total += int((end - start).total_seconds())
        return total

    @staticmethod
    def _building_help_progress(adventurer_id: int, since: datetime, template: dict) -> dict:
        required = _required_help_seconds(template)
        accumulated = AdventurerS1TaskService._sum_building_help_seconds(adventurer_id, since)
        remaining = max(0, required - accumulated)
        return {
            'help_required_seconds': required,
            'help_progress_seconds': min(accumulated, required),
            'help_remaining_seconds': remaining,
        }

    @staticmethod
    def _gathering_session_seconds(inst, session_end: datetime) -> int:
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService

        config = BuildingSettlementService.get_reward_config(inst)
        return int(config.get('usage_duration_minutes', 30) or 30) * 60

    @staticmethod
    def _sum_gathering_seconds(adventurer_id: int, since: datetime) -> int:
        """统计冒险者自 since 起在采集区（伐木林/采石场）的累计在场秒数。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1,
            AdventurerS1BuildingInstance,
            AdventurerS1BuildingTemplate,
            AdventurerS1BuildingUsageLog,
        )

        now = _portable_utc_now()
        total = 0
        seen_log_ids: set[int] = set()

        def _add_session_overlap(inst, session_end: datetime) -> None:
            nonlocal total
            duration_sec = AdventurerS1TaskService._gathering_session_seconds(inst, session_end)
            session_start = session_end - timedelta(seconds=duration_sec)
            start = max(session_start, since)
            end = session_end
            if end <= start:
                return
            total += int((end - start).total_seconds())

        usage_logs = (
            AdventurerS1BuildingUsageLog.query
            .join(
                AdventurerS1BuildingInstance,
                AdventurerS1BuildingUsageLog.building_instance_id == AdventurerS1BuildingInstance.id,
            )
            .join(
                AdventurerS1BuildingTemplate,
                AdventurerS1BuildingInstance.template_id == AdventurerS1BuildingTemplate.id,
            )
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id,
                AdventurerS1BuildingTemplate.category == 'gathering',
                AdventurerS1BuildingUsageLog.used_at > since,
            )
            .all()
        )
        for log in usage_logs:
            if log.id in seen_log_ids:
                continue
            seen_log_ids.add(log.id)
            _add_session_overlap(log.building_instance, log.used_at)

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if (
            adv
            and adv.status == 'building'
            and adv.building_instance_id
            and adv.building_assigned_at
        ):
            inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, adv.building_instance_id)
            if inst and inst.template and inst.template.category == 'gathering':
                start = max(adv.building_assigned_at, since)
                if now > start:
                    total += int((now - start).total_seconds())

        return total

    @staticmethod
    def _gathering_progress(adventurer_id: int, since: datetime, template: dict) -> dict:
        required = _required_help_seconds(template)
        accumulated = AdventurerS1TaskService._sum_gathering_seconds(adventurer_id, since)
        remaining = max(0, required - accumulated)
        return {
            'gathering_required_seconds': required,
            'gathering_progress_seconds': min(accumulated, required),
            'gathering_remaining_seconds': remaining,
        }

    @staticmethod
    def _count_work_sessions(adventurer_id: int, since: datetime) -> int:
        """统计冒险者自 since 起在打工区完成的次数。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1BuildingInstance,
            AdventurerS1BuildingTemplate,
            AdventurerS1BuildingUsageLog,
        )

        return (
            AdventurerS1BuildingUsageLog.query
            .join(
                AdventurerS1BuildingInstance,
                AdventurerS1BuildingUsageLog.building_instance_id == AdventurerS1BuildingInstance.id,
            )
            .join(
                AdventurerS1BuildingTemplate,
                AdventurerS1BuildingInstance.template_id == AdventurerS1BuildingTemplate.id,
            )
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id,
                AdventurerS1BuildingTemplate.category == 'work',
                AdventurerS1BuildingUsageLog.used_at > since,
            )
            .count()
        )

    @staticmethod
    def _work_progress(adventurer_id: int, since: datetime, template: dict) -> dict:
        required = _work_required_sessions(template)
        completed = AdventurerS1TaskService._count_work_sessions(adventurer_id, since)
        remaining = max(0, required - completed)
        return {
            'work_required_sessions': required,
            'work_progress_sessions': min(completed, required),
            'work_remaining_sessions': remaining,
        }

    @staticmethod
    def _sum_contribution_gained(adventurer_id: int, since: datetime) -> int:
        """接取任务后累计获得的贡献值（读 contribution_total 正流水，排除 GM 校准）。"""
        from plugins.adventurer_s1.models import AdventurerS1CurrencyLog
        from plugins.adventurer_s1.services.s1_contribution_service import CONTRIBUTION_TOTAL_CODE

        total = db_manager.query(func.coalesce(func.sum(AdventurerS1CurrencyLog.change_amount), 0)).filter(
            AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
            AdventurerS1CurrencyLog.currency_code == CONTRIBUTION_TOTAL_CODE,
            AdventurerS1CurrencyLog.change_amount > 0,
            AdventurerS1CurrencyLog.created_at >= since,
            ~AdventurerS1CurrencyLog.source_type.in_(_CONTRIBUTION_TASK_EXCLUDED_SOURCES),
        ).scalar()
        return int(round(float(total or 0)))

    @staticmethod
    def _contribution_progress(adventurer_id: int, since: datetime, template: dict) -> dict:
        required = _required_contribution(template)
        accumulated = AdventurerS1TaskService._sum_contribution_gained(adventurer_id, since)
        remaining = max(0, required - accumulated)
        return {
            'contribution_required': required,
            'contribution_progress': min(accumulated, required),
            'contribution_remaining': remaining,
        }

    @staticmethod
    def _count_completions(task_template_id: str) -> int:
        """统计某任务已完成次数。"""
        return _task_instance_query(task_template_id).filter_by(status='completed').count()

    @staticmethod
    def _count_occupied_slots(task_template_id: str) -> int:
        """统计已占用名额（进行中 + 已完成；放弃不占名额）。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance
        return _task_instance_query(task_template_id).filter(
            AdventurerS1TaskInstance.status.in_(('accepted', 'completed')),
        ).count()

    @staticmethod
    def _adventurer_has_completed(task_template_id: str, adventurer_id: int) -> bool:
        """冒险者是否已完成过该任务（完成后不可再次领取；放弃后可重试）。"""
        return _task_instance_query(task_template_id).filter_by(
            adventurer_id=adventurer_id,
            status='completed',
        ).first() is not None

    @staticmethod
    def _is_task_ended(template: dict, stats_map: dict = None) -> bool:
        if _is_hidden_boss_task(template):
            return AdventurerS1TaskService.hidden_boss_progress()['ended']
        max_comp = _task_max_completions(template)
        if not max_comp:
            return False
        if stats_map is not None:
            return _task_stat(stats_map, template['id'], 'completed') >= max_comp
        return AdventurerS1TaskService._count_completions(template['id']) >= max_comp

    @staticmethod
    def hidden_boss_progress() -> dict:
        from plugins.adventurer_s1.services.s1_hidden_boss_service import S1HiddenBossService

        return S1HiddenBossService.progress()

    @staticmethod
    def is_hidden_boss_ended() -> bool:
        return bool(AdventurerS1TaskService.hidden_boss_progress()['ended'])

    @staticmethod
    def _member_took_damage(adv_id: int, member_updates: list) -> bool:
        """根据探险结算 member_updates 判断该成员是否受伤或死亡。"""
        for u in member_updates or []:
            if not isinstance(u, dict):
                continue
            try:
                mid = int(u.get('id', -1))
            except (TypeError, ValueError):
                continue
            if mid != adv_id:
                continue
            if u.get('is_dead'):
                return True
            hp_change = int(u.get('hp_change', 0) or 0)
            return hp_change < 0
        return True

    @staticmethod
    def _format_required_dungeon_label(template: dict) -> Optional[str]:
        if _requires_random_solo_match(template):
            return '单排随机地图'
        if template.get('required_dungeon'):
            return str(template['required_dungeon'])
        req_min = template.get('required_dungeon_min_level')
        req_max = template.get('required_dungeon_max_level')
        if req_min is None:
            return None
        if req_max is not None and int(req_max) >= 999:
            return f'Lv.{req_min}+'
        if req_max is not None and int(req_max) != int(req_min):
            return f'Lv.{req_min}-{req_max}'
        return f'Lv.{req_min}+'

    @staticmethod
    def _dungeon_matches_level_requirement(target_dungeon: str, template: dict) -> bool:
        req_min = template.get('required_dungeon_min_level')
        req_max = template.get('required_dungeon_max_level')
        if req_min is None:
            return True
        from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_dungeon_level_range
        d_min, d_max = get_s1_dungeon_level_range(target_dungeon)
        if int(d_min) != int(req_min):
            return False
        if req_max is None:
            return True
        if int(req_max) >= 999:
            return int(d_max) >= 999
        return int(d_max) == int(req_max)

    @staticmethod
    def _expedition_satisfies(
        template: dict, target_dungeon: str, adv_id: int, member_updates: list,
        expedition_log_id: int | None = None,
    ) -> bool:
        required = template.get('required_dungeon')
        if required and required != target_dungeon:
            return False
        if not AdventurerS1TaskService._dungeon_matches_level_requirement(target_dungeon, template):
            return False
        if _requires_random_solo_match(template) and not AdventurerS1TaskService._log_is_random_solo_match(
            expedition_log_id, adv_id,
        ):
            return False
        if template.get('no_damage'):
            return not AdventurerS1TaskService._member_took_damage(adv_id, member_updates)
        return bool(
            required
            or template.get('required_dungeon_min_level') is not None
            or template.get('no_damage')
            or _requires_random_solo_match(template)
        )

    @staticmethod
    def _log_is_random_solo_match(expedition_log_id: int | None, adventurer_id: int) -> bool:
        """该探险是否来自当前冒险者自己的单人随机匹配报名。"""
        if not expedition_log_id:
            return False
        from plugins.adventurer_s1.models import (
            AdventurerS1ExpeditionLog,
            AdventurerS1MatchTicket,
            AdventurerS1MatchTicketMember,
        )

        row = (
            db_manager.query(AdventurerS1MatchTicket.id)
            .join(
                AdventurerS1MatchTicketMember,
                AdventurerS1MatchTicketMember.ticket_id == AdventurerS1MatchTicket.id,
            )
            .join(
                AdventurerS1ExpeditionLog,
                AdventurerS1ExpeditionLog.party_id == AdventurerS1MatchTicket.matched_party_id,
            )
            .filter(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
                AdventurerS1MatchTicketMember.adventurer_id == int(adventurer_id),
                AdventurerS1MatchTicket.queue_type == 'solo',
                AdventurerS1MatchTicket.preferred_dungeon.is_(None),
                _json_array_is_empty_or_null(AdventurerS1MatchTicket.preferred_dungeons),
            )
            .first()
        )
        if row is not None:
            return True

        # 探险结束后队伍会被删除，matched_party_id 的 ON DELETE SET NULL 会让上面的
        # 事后关联失效。匹配票的生命周期仍完整包住探险日志创建时刻，可据此稳定确认
        # 当前成员是否由随机单排匹配进入本次探险。
        expedition_log = db_manager.query(
            AdventurerS1ExpeditionLog.created_at,
        ).filter(
            AdventurerS1ExpeditionLog.id == int(expedition_log_id),
        ).first()
        if not expedition_log or not expedition_log.created_at:
            return False

        row = (
            db_manager.query(AdventurerS1MatchTicket.id)
            .join(
                AdventurerS1MatchTicketMember,
                AdventurerS1MatchTicketMember.ticket_id == AdventurerS1MatchTicket.id,
            )
            .filter(
                AdventurerS1MatchTicketMember.adventurer_id == int(adventurer_id),
                AdventurerS1MatchTicket.queue_type == 'solo',
                AdventurerS1MatchTicket.preferred_dungeon.is_(None),
                _json_array_is_empty_or_null(AdventurerS1MatchTicket.preferred_dungeons),
                AdventurerS1MatchTicket.matched_at.isnot(None),
                AdventurerS1MatchTicket.matched_at <= expedition_log.created_at,
                AdventurerS1MatchTicket.cancelled_at.isnot(None),
                AdventurerS1MatchTicket.cancelled_at >= expedition_log.created_at,
            )
            .first()
        )
        return row is not None

    @staticmethod
    def _build_task_list_item(
        t: dict,
        adv=None,
        active=None,
        stats_map: dict = None,
        completed_task_ids: Optional[set[str]] = None,
        completed_today_daily_reset_sources: Optional[set[str]] = None,
        tavern_seated: bool = False,
    ) -> dict:
        """组装单个任务列表项（含可接取判定）。

        Args:
            stats_map: SQL 聚合后的任务完成数与占用数。
            completed_task_ids: 当前冒险者已完成过的任务 ID。
        """
        req = t.get('requirements') or {}
        can_accept = True
        reason = None
        max_comp = _task_max_completions(t)
        if max_comp and stats_map is not None:
            completed = _task_stat(stats_map, t['id'], 'completed')
            occupied = _task_stat(stats_map, t['id'], 'occupied')
        else:
            completed = AdventurerS1TaskService._count_completions(t['id']) if max_comp else None
            occupied = AdventurerS1TaskService._count_occupied_slots(t['id']) if max_comp else None
        if _is_hidden_boss_task(t):
            progress = _cached_hidden_boss_progress()
            completed = progress['defeated']
            remaining = progress['remaining']
            is_ended = progress['ended']
        elif max_comp and _completion_limited_unlimited_accept(t):
            remaining = max(0, max_comp - (completed or 0))
        else:
            remaining = max(0, max_comp - occupied) if max_comp else None
        if not _is_hidden_boss_task(t):
            is_ended = bool(max_comp and completed >= max_comp)

        if not adv:
            pass
        elif active:
            can_accept = False
            reason = '已有进行中的任务'
        elif is_ended:
            can_accept = False
            reason = '任务已结束'
        elif adv.status == 'exploring':
            can_accept = False
            reason = '探险中无法接取任务'
        elif (
            _daily_reset_source_id(t['id'])
            and (
                (
                    completed_today_daily_reset_sources is not None
                    and _daily_reset_source_id(t['id']) in completed_today_daily_reset_sources
                )
                or (
                    completed_today_daily_reset_sources is None
                    and adv
                    and _adventurer_completed_today(_daily_reset_source_id(t['id']), adv.id)
                )
            )
        ):
            can_accept = False
            reason = '今日已完成该任务'
        elif (
            (completed_task_ids is not None and t['id'] in completed_task_ids)
            or (completed_task_ids is None and AdventurerS1TaskService._adventurer_has_completed(t['id'], adv.id))
        ) and not _is_repeatable_task(t) and not _daily_reset_source_id(t['id']):
            can_accept = False
            reason = '你已完成过该任务'
        elif max_comp and occupied >= max_comp and not _completion_limited_unlimited_accept(t):
            can_accept = False
            reason = '任务名额已满' if not is_ended else '任务已结束'
        elif req.get('min_level', 0) > 0 and (adv.level or 1) < req['min_level']:
            can_accept = False
            reason = f"等级不足（需要 {req['min_level']}，当前 {adv.level or 1}）"
        elif req.get('required_stat') and req.get('required_stat_min', 0) > 0:
            stat = req['required_stat']
            val = int(getattr(adv, stat, 0) or 0)
            if val < req['required_stat_min']:
                can_accept = False
                stat_cn = AdventurerS1TaskService.STAT_NAMES.get(stat, stat)
                reason = f"{stat_cn}不足（需要 {req['required_stat_min']}，当前 {val}）"
        elif req.get('stamina_cost', 0) > 0:
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
            current_stamina = get_live_stamina(
                adv,
                max_stamina=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)),
                tavern_seated=tavern_seated,
            )
            if current_stamina < req['stamina_cost']:
                can_accept = False
                reason = f"精力不足（需要 {req['stamina_cost']}，当前 {current_stamina}）"
        elif _is_building_help_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_gathering_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_work_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_contribution_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_treasure_hunt_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_hidden_boss_task(t):
            if adv.status == 'exploring':
                can_accept = False
                reason = '探险中无法接取任务'
            elif adv.status == 'dead':
                can_accept = False
                reason = '冒险者已阵亡'
        elif _is_expedition_task(t):
            if adv.status in ('building', 'dead'):
                can, dispatch_reason = adv.can_dispatch()
                can_accept = can
                reason = dispatch_reason
        else:
            can, dispatch_reason = adv.can_dispatch()
            if not can:
                can_accept = False
                reason = dispatch_reason

        item = {
            'id': t['id'],
            'name': t.get('name'),
            'description': t.get('description'),
            'icon': t.get('icon'),
            'category': t.get('category'),
            'requirements': _requirements_for_client(req),
            'duration_minutes': t.get('duration_minutes', 0),
            # Metadata enrichment is batched after assembling the complete list.
            'rewards': _enrich_rewards_for_display(t.get('rewards'), {}),
            'narrative': t.get('narrative'),
            'completion_type': _completion_type(t),
            'can_accept': can_accept,
            'reject_reason': reason,
            'is_ended': is_ended,
        }
        if _is_expedition_task(t):
            item['required_dungeon'] = t.get('required_dungeon')
            if _requires_random_solo_match(t):
                item['required_random_solo_match'] = True
                item['required_dungeon_label'] = AdventurerS1TaskService._format_required_dungeon_label(t)
            if t.get('required_dungeon_min_level') is not None:
                item['required_dungeon_min_level'] = t.get('required_dungeon_min_level')
                item['required_dungeon_max_level'] = t.get('required_dungeon_max_level')
                item['required_dungeon_label'] = AdventurerS1TaskService._format_required_dungeon_label(t)
            if t.get('no_damage'):
                item['no_damage'] = True
        if _is_building_help_task(t):
            item['help_required_seconds'] = _required_help_seconds(t)
        if _is_gathering_task(t):
            item['gathering_required_seconds'] = _required_help_seconds(t)
        if _is_work_task(t):
            item['work_required_sessions'] = _work_required_sessions(t)
        if _is_contribution_task(t):
            item['contribution_required'] = _required_contribution(t)
        if _is_treasure_hunt_task(t):
            item['treasure_maps_remaining'] = remaining
            item['reward_drop_mode'] = 'random_one'
            rewards = dict(item.get('rewards') or {})
            pool_items: list = []
            names = set()
            for entry in (rewards.get('items') or []):
                name = _reward_item_entry_name(entry)
                if not name or name in names:
                    continue
                names.add(name)
                pool_items.append({'name': name, 'quantity': 1})
            if pool_items:
                # 与普通奖励一起由列表出口批量补元数据；角标无需查物品表。
                rewards['items'] = pool_items
                rewards['drop_mode'] = 'random_one'
                item['rewards'] = rewards
        if _is_hidden_boss_task(t):
            item['hidden_boss_target_label'] = '隐藏 Boss 讨伐'
            item['hidden_boss_progress_count'] = completed
            item['hidden_boss_total_count'] = max_comp or progress['total']
            item['hidden_boss_remaining_count'] = remaining
        if _is_repeatable_task(t):
            item['repeatable'] = True
        if max_comp:
            item['max_completions'] = max_comp
            item['completion_count'] = completed
            item['completion_remaining'] = remaining
            # 前端兼容字段
            item['max_acceptances'] = max_comp
            item['acceptance_count'] = occupied
            item['acceptance_remaining'] = remaining
        return item

    @staticmethod
    def _is_acceptable_task_badge(item: dict) -> bool:
        """任务栏角标：是否计入「可接任务」数量。"""
        if not item.get('id'):
            return False
        if item.get('can_accept') is False:
            return False
        if item.get('reject_reason') in ('你已完成过该任务', '今日已完成该任务'):
            return False
        if item.get('is_ended'):
            return False
        max_comp = item.get('max_completions')
        if max_comp is not None and (item.get('completion_remaining') or 0) <= 0:
            return False
        return True

    @staticmethod
    def _is_player_visible_task_item(item: dict) -> bool:
        """普通玩家任务列表可见性；不要把仅前端隐藏的任务下发到浏览器。"""
        if not item.get('id'):
            return False
        # 每日任务完成后保留到当日结束，让自动结算结果可追溯；永久一次性任务仍隐藏。
        if item.get('reject_reason') == '你已完成过该任务':
            return False
        if item.get('is_ended'):
            return False
        max_comp = item.get('max_completions')
        if max_comp is not None and (item.get('completion_remaining') or 0) <= 0:
            return False
        return True

    @staticmethod
    def has_active_accepted_task(adventurer_id: int) -> bool:
        """读路径角标用：仅判断是否存在 accepted 实例，不触发补结算/写库。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance

        row = (
            db_manager.query(AdventurerS1TaskInstance.id)
            .filter_by(adventurer_id=adventurer_id, status='accepted')
            .first()
        )
        return row is not None

    @staticmethod
    def summarize_acceptable_tasks(adventurer_id: int) -> Tuple[int, list[str], Optional[str]]:
        """角标/首页用：只统计可接任务数与 ID，不 enrich 奖励、不跑过期维护写库。"""
        cached = _summarize_acceptable_cache.get(adventurer_id)
        if cached is not None:
            return cached

        from sqlalchemy.orm import defer, joinedload, lazyload, noload
        from plugins.adventurer_s1.models import (
            AdventurerS1,
            AdventurerS1BuildingInstance,
            AdventurerS1DormRoom,
        )

        adv = (
            AdventurerS1.query.session.get(AdventurerS1, adventurer_id, options=(joinedload(AdventurerS1.building_instance).options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers)), joinedload(AdventurerS1.dorm_room).options(noload(AdventurerS1DormRoom.occupants), joinedload(AdventurerS1DormRoom.building_instance).options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers))), defer(AdventurerS1.history_story), lazyload(AdventurerS1.active_title_config), noload(AdventurerS1.traits), noload(AdventurerS1.equipment_items), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.materials), noload(AdventurerS1.story_threads),))
        )
        if not adv:
            return 0, [], '冒险者不存在'
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            resolve_tavern_seated_for_recovery,
        )
        tavern_seated = resolve_tavern_seated_for_recovery(adv)

        override_map = _preload_all_overrides()
        templates, _ = _load_templates()
        runtime_map = _runtime_templates_map(include_disabled=False, override_map=override_map)
        limited_task_ids = {
            task_id for task_id, template in runtime_map.items()
            if _task_max_completions(template)
        }
        stats_map, completed_task_ids, completed_today_daily_reset_sources = _load_task_instance_stats(
            adventurer_id, task_template_ids=limited_task_ids,
        )

        acceptable_ids: list[str] = []
        seen_template_ids: set[str] = set()

        def consider(template: dict) -> None:
            item = AdventurerS1TaskService._build_task_list_item(
                template, adv=adv, active=None,
                stats_map=stats_map, completed_task_ids=completed_task_ids,
                completed_today_daily_reset_sources=completed_today_daily_reset_sources,
                tavern_seated=tavern_seated,
            )
            if AdventurerS1TaskService._is_acceptable_task_badge(item):
                acceptable_ids.append(item['id'])

        for base in templates:
            task_id = base['id']
            if task_id not in runtime_map:
                continue
            seen_template_ids.add(task_id)
            consider(runtime_map[task_id])
        for task_id, effective in runtime_map.items():
            if task_id in seen_template_ids:
                continue
            consider(effective)

        result = (len(acceptable_ids), sorted(acceptable_ids), None)
        _summarize_acceptable_cache.set(adventurer_id, result)
        return result

    # ── 查询 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def get_available_tasks(adventurer_id: Optional[int] = None) -> Tuple[Optional[list], Optional[str]]:
        """返回所有任务模板。adventurer_id 可选，传入时标记是否满足接取条件。"""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        adv = None
        active = None
        if adventurer_id:
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            if not adv:
                return None, '冒险者不存在'
            active = AdventurerS1TaskInstance.query.filter_by(
                adventurer_id=adventurer_id, status='accepted',
            ).first()
            if active:
                return [], None
        return AdventurerS1TaskService._build_available_tasks(
            adventurer_id=adventurer_id,
            adv=adv,
            active=active,
            expire_ended=True,
        )

    @staticmethod
    def _build_available_tasks(
        *,
        adventurer_id: Optional[int],
        adv,
        active,
        expire_ended: bool,
    ) -> Tuple[Optional[list], Optional[str]]:
        """Build the task list from a caller-provided adventurer/task snapshot."""
        tavern_seated = False
        if adv is not None:
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                resolve_tavern_seated_for_recovery,
            )
            tavern_seated = resolve_tavern_seated_for_recovery(adv)

        # 覆盖层体量随模板数增长；实例历史则只对有限额任务做 SQL 聚合。
        override_map = _preload_all_overrides()
        templates, _ = _load_templates()
        runtime_map = _runtime_templates_map(include_disabled=False, override_map=override_map)
        limited_task_ids = {
            task_id for task_id, template in runtime_map.items()
            if _task_max_completions(template)
        }
        stats_map, completed_task_ids, completed_today_daily_reset_sources = _load_task_instance_stats(
            adventurer_id, task_template_ids=limited_task_ids,
        )
        if expire_ended:
            for task_id, effective in runtime_map.items():
                if _is_treasure_hunt_task(effective) and AdventurerS1TaskService.is_treasure_hunt_ended(
                    task_id, override_map=override_map, stats_map=stats_map,
                ):
                    AdventurerS1TaskService.expire_remaining_accepted_tasks(task_id)
                if _is_hidden_boss_task(effective) and AdventurerS1TaskService.is_hidden_boss_ended():
                    AdventurerS1TaskService.expire_nonclaimable_hidden_boss_tasks(task_id)
        result = []
        for base in templates:
            task_id = base['id']
            if task_id not in runtime_map:
                continue
            result.append(
                AdventurerS1TaskService._build_task_list_item(
                    runtime_map[task_id], adv=adv, active=active,
                    stats_map=stats_map, completed_task_ids=completed_task_ids,
                    completed_today_daily_reset_sources=completed_today_daily_reset_sources,
                    tavern_seated=tavern_seated,
                )
            )
        for task_id, effective in runtime_map.items():
            if task_id in {t['id'] for t in templates}:
                continue
            result.append(
                AdventurerS1TaskService._build_task_list_item(
                    effective, adv=adv, active=active,
                    stats_map=stats_map, completed_task_ids=completed_task_ids,
                    completed_today_daily_reset_sources=completed_today_daily_reset_sources,
                    tavern_seated=tavern_seated,
                )
            )
        _enrich_task_list_rewards(result)
        result = [item for item in result if AdventurerS1TaskService._is_player_visible_task_item(item)]
        return result, None

    @staticmethod
    def get_active_task(
        adventurer_id: int,
        *,
        instance=None,
        reconcile: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """获取冒险者的活跃任务实例（含模板信息）。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance

        if instance is None:
            instance = AdventurerS1TaskInstance.query.filter_by(
                adventurer_id=adventurer_id, status='accepted',
            ).first()
        if not instance:
            if reconcile:
                AdventurerS1TaskService._cleanup_terminal_task_board_occupancy(adventurer_id)
            return None, None

        # 预加载 overrides，避免后续逐条查
        override_map = _preload_all_overrides()

        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(instance.task_template_id, override_map=override_map)
        if not template:
            template = templates_map.get(instance.task_template_id, {})
        if reconcile and _is_expedition_task(template):
            reconciled, reconcile_err = AdventurerS1TaskService.reconcile_stale_expedition_task(instance)
            if reconcile_err:
                logger.warning(
                    '探险任务补结算失败 adv=%s task=%s err=%s',
                    adventurer_id, instance.task_template_id, reconcile_err,
                )
            if reconciled:
                AdventurerS1TaskService._cleanup_terminal_task_board_occupancy(adventurer_id)
                return None, None
            instance = AdventurerS1TaskInstance.query.filter_by(
                adventurer_id=adventurer_id, status='accepted',
            ).first()
            if not instance:
                AdventurerS1TaskService._cleanup_terminal_task_board_occupancy(adventurer_id)
                return None, None
            template, _ = _resolve_effective_template_by_id(instance.task_template_id, override_map=override_map)
            if not template:
                template = templates_map.get(instance.task_template_id, {})
        if reconcile and _is_treasure_hunt_task(template) and AdventurerS1TaskService.is_treasure_hunt_ended(
            instance.task_template_id, override_map=override_map,
        ):
            AdventurerS1TaskService.expire_remaining_accepted_tasks(instance.task_template_id)
            return None, None
        if reconcile and _is_hidden_boss_task(template):
            reconciled, reconcile_err = AdventurerS1TaskService.reconcile_stale_hidden_boss_task(
                instance,
                template,
            )
            if reconcile_err:
                logger.warning(
                    '隐藏Boss任务补结算失败 adv=%s task=%s err=%s',
                    adventurer_id, instance.task_template_id, reconcile_err,
                )
            if reconciled:
                return None, None
        if reconcile and _is_hidden_boss_task(template) and AdventurerS1TaskService.is_hidden_boss_ended():
            AdventurerS1TaskService.expire_nonclaimable_hidden_boss_tasks(
                instance.task_template_id,
            )
            instance = AdventurerS1TaskInstance.query.filter_by(
                id=instance.id, status='accepted',
            ).first()
            if not instance:
                return None, None
        data = instance.to_dict(
            template=AdventurerS1TaskService._template_for_client(template),
        )
        if _is_expedition_task(template):
            data['required_dungeon'] = template.get('required_dungeon')
            if _requires_random_solo_match(template):
                data['required_random_solo_match'] = True
                data['required_dungeon_label'] = AdventurerS1TaskService._format_required_dungeon_label(template)
            if template.get('required_dungeon_min_level') is not None:
                data['required_dungeon_min_level'] = template.get('required_dungeon_min_level')
                data['required_dungeon_max_level'] = template.get('required_dungeon_max_level')
                data['required_dungeon_label'] = AdventurerS1TaskService._format_required_dungeon_label(template)
            data['completion_type'] = 'expedition'
            if template.get('no_damage'):
                data['no_damage'] = True
        elif _is_building_help_task(template):
            data['completion_type'] = 'building_help'
            data.update(AdventurerS1TaskService._building_help_progress(
                instance.adventurer_id, instance.started_at, template,
            ))
        elif _is_gathering_task(template):
            data['completion_type'] = 'gathering'
            data.update(AdventurerS1TaskService._gathering_progress(
                instance.adventurer_id, instance.started_at, template,
            ))
        elif _is_work_task(template):
            data['completion_type'] = 'work'
            data.update(AdventurerS1TaskService._work_progress(
                instance.adventurer_id, instance.started_at, template,
            ))
        elif _is_contribution_task(template):
            data['completion_type'] = 'contribution'
            data.update(AdventurerS1TaskService._contribution_progress(
                instance.adventurer_id, instance.started_at, template,
            ))
        elif _is_treasure_hunt_task(template):
            data['completion_type'] = 'treasure_hunt'
        elif _is_hidden_boss_task(template):
            progress = AdventurerS1TaskService.hidden_boss_progress()
            data['completion_type'] = 'hidden_boss'
            data['hidden_boss_target_label'] = '隐藏 Boss 讨伐'
            data['hidden_boss_progress_count'] = progress['defeated']
            data['hidden_boss_total_count'] = progress['total']
            data['hidden_boss_remaining_count'] = progress['remaining']
        else:
            data['completion_type'] = 'timer'
        return data, None

    @staticmethod
    def get_player_task_board_data(
        user_id: int,
        adventurer_id: Optional[int] = None,
        *, include_tasks: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """玩家读入口：先确认所有权，再读取个人任务；省略角色时只返回公共模板。"""
        if adventurer_id is not None:
            from plugins.adventurer_s1.models import AdventurerS1

            owned = AdventurerS1.query.with_entities(AdventurerS1.id).filter_by(
                id=adventurer_id, owner_id=user_id,
            ).first()
            if not owned:
                return None, '只能查看自己冒险者的任务'
        return AdventurerS1TaskService.get_task_board_data(
            adventurer_id, include_tasks=include_tasks,
        )

    @staticmethod
    def get_task_board_data(
        adventurer_id: Optional[int] = None, *, include_tasks: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """Return a consistent, read-only task-board snapshot with one active-instance lookup."""
        if not adventurer_id:
            tasks, err = AdventurerS1TaskService._build_available_tasks(
                adventurer_id=None, adv=None, active=None, expire_ended=False,
            )
            return ({'tasks': tasks or [], 'active_task': None}, None) if not err else (None, err)

        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        active = AdventurerS1TaskInstance.query.filter_by(
            adventurer_id=adventurer_id,
            status='accepted',
        ).first()
        if active:
            active_task, err = AdventurerS1TaskService.get_active_task(
                adventurer_id,
                instance=active,
                reconcile=False,
            )
            if err:
                return None, err
            return {'tasks': [], 'active_task': active_task}, None

        if not include_tasks:
            return {'tasks': [], 'active_task': None}, None

        tasks, err = AdventurerS1TaskService._build_available_tasks(
            adventurer_id=adventurer_id,
            adv=adv,
            active=None,
            expire_ended=False,
        )
        if err:
            return None, err
        return {'tasks': tasks or [], 'active_task': None}, None

    # ── 接取 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_terminal_task_board_occupancy(adventurer_id: int) -> bool:
        """Release task-board occupancy left behind by terminal task instances."""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv or adv.status != 'building' or not adv.building_instance_id:
            return False

        stale_instance = AdventurerS1TaskInstance.query.filter(
            AdventurerS1TaskInstance.adventurer_id == adventurer_id,
            AdventurerS1TaskInstance.building_instance_id == adv.building_instance_id,
            AdventurerS1TaskInstance.status.in_(('completed', 'abandoned', 'expired')),
        ).order_by(AdventurerS1TaskInstance.id.desc()).first()
        if not stale_instance:
            return False

        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, with_for_update=True)
                if (
                    not locked_adv
                    or locked_adv.status != 'building'
                    or locked_adv.building_instance_id != stale_instance.building_instance_id
                ):
                    return False
                from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Svc
                Svc.enter_rest(locked_adv)
                locked_adv.building_instance_id = None
                locked_adv.building_assigned_at = None
                locked_adv.building_usage_extra = None
            return True
        except Exception as e:
            logger.warning('cleanup stale task-board occupancy failed adv=%s err=%s', adventurer_id, e)
            return False

    @staticmethod
    def accept_task(
        adventurer_id: int, task_template_id: str, building_instance_id: int,
        user_id: Optional[int] = None, is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """接取任务：检查条件、创建实例；timer 类型会占用冒险者，expedition 类型不占用。"""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1BuildingInstance, AdventurerS1TaskInstance

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能操作自己拥有的冒险者'
        if adv.status == 'exploring':
            return None, '探险中无法接取任务'

        existing = AdventurerS1TaskInstance.query.filter_by(
            adventurer_id=adventurer_id, status='accepted',
        ).first()
        if existing:
            return None, '已有进行中的任务，请先完成或放弃'

        runtime_map = _runtime_templates_map(include_disabled=False)
        if task_template_id not in runtime_map:
            return None, f'任务不存在: {task_template_id}'
        template = runtime_map[task_template_id]

        blocked, block_msg = _adventurer_accept_blocked(template, task_template_id, adventurer_id)
        if blocked:
            return None, block_msg

        max_comp = _task_max_completions(template)
        if max_comp:
            if _is_hidden_boss_task(template):
                if AdventurerS1TaskService.is_hidden_boss_ended():
                    return None, '任务已结束'
            elif AdventurerS1TaskService._count_completions(task_template_id) >= max_comp:
                return None, '任务已结束'
            if (
                not _completion_limited_unlimited_accept(template)
                and AdventurerS1TaskService._count_occupied_slots(task_template_id) >= max_comp
            ):
                return None, '任务名额已满'

        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return None, '建筑不存在'
        if inst.status != 'built':
            return None, '建筑尚未建成'

        req = template.get('requirements') or {}
        if req.get('min_level', 0) > 0 and (adv.level or 1) < req['min_level']:
            return None, f"等级不足（需要 {req['min_level']}）"

        if req.get('required_stat') and req.get('required_stat_min', 0) > 0:
            stat = req['required_stat']
            val = int(getattr(adv, stat, 0) or 0)
            if val < req['required_stat_min']:
                stat_cn = AdventurerS1TaskService.STAT_NAMES.get(stat, stat)
                return None, f"{stat_cn}不足（需要 {req['required_stat_min']}，当前 {val}）"

        stamina_cost = req.get('stamina_cost', 0)
        expedition_task = _is_expedition_task(template)
        building_help_task = _is_building_help_task(template)
        gathering_task = _is_gathering_task(template)
        work_task = _is_work_task(template)
        contribution_task = _is_contribution_task(template)
        treasure_hunt_task = _is_treasure_hunt_task(template)
        hidden_boss_task = _is_hidden_boss_task(template)
        free_accept_task = (
            expedition_task or building_help_task or gathering_task or work_task
            or contribution_task or treasure_hunt_task or hidden_boss_task
        )

        if adv.status == 'activity':
            return None, '参加酒馆活动中，暂时无法接取任务'
        if not free_accept_task:
            can, reason = adv.can_dispatch()
            if not can:
                return None, reason
        elif (
            building_help_task or gathering_task or work_task or contribution_task
            or treasure_hunt_task or hidden_boss_task
        ):
            if adv.status == 'exploring':
                return None, '探险中无法接取任务'
            if adv.status == 'dead':
                return None, '冒险者已阵亡'
        elif adv.status in ('exploring', 'building', 'dead'):
            can, reason = adv.can_dispatch()
            if not can:
                return None, reason

        if stamina_cost > 0:
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
            current_stamina = get_live_stamina(
                adv,
                max_stamina=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)),
            )
            if current_stamina < stamina_cost:
                return None, f'精力不足（需要 {stamina_cost}，当前 {current_stamina}）'

        try:
            with db_manager.transaction():
                if max_comp:
                    if _is_hidden_boss_task(template):
                        if AdventurerS1TaskService.is_hidden_boss_ended():
                            return None, '任务已结束'
                    elif AdventurerS1TaskService._count_completions(task_template_id) >= max_comp:
                        return None, '任务已结束'
                    if (
                        not _completion_limited_unlimited_accept(template)
                        and AdventurerS1TaskService._count_occupied_slots(task_template_id) >= max_comp
                    ):
                        return None, '任务名额已满'

                task_instance = AdventurerS1TaskInstance(
                    adventurer_id=adventurer_id,
                    task_template_id=task_template_id,
                    building_instance_id=building_instance_id,
                    status='accepted',
                    started_at=_portable_utc_now(),
                )
                db_manager.add(task_instance)

                if not free_accept_task:
                    # 行锁 + 状态重检：与并发组队/发车串行化，防止用事务外的陈旧快照把
                    # status 覆盖成 building 却遗留 PartyMember 孤儿（退不掉队的根因之一）。
                    from plugins.adventurer_s1.models import AdventurerS1
                    locked_adv = AdventurerS1.query.filter_by(
                        id=adventurer_id,
                    ).with_for_update().first()
                    if not locked_adv:
                        return None, '冒险者不存在'
                    can, reason = locked_adv.can_dispatch()
                    if not can:
                        return None, reason
                    adv = locked_adv
                    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Svc
                    Svc.leave_rest(adv, 'building', _portable_utc_now())
                    if stamina_cost > 0:
                        adv.stamina = max(0, adv.stamina - stamina_cost)
                    adv.building_instance_id = inst.id
                    adv.building_assigned_at = _portable_utc_now()
                elif stamina_cost > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Svc
                    Svc.leave_rest(adv, adv.status, _portable_utc_now())
                    adv.stamina = max(0, adv.stamina - stamina_cost)
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

        _invalidate_owner_home_badges(adv)
        return task_instance.to_dict(
            template=AdventurerS1TaskService._template_for_client(template),
        ), None

    @staticmethod
    def abandon_task(
        task_instance_id: int,
        user_id: Optional[int] = None, is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """放弃进行中的任务；放弃释放全服名额，完成后仍不可重复领取。"""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        instance = AdventurerS1TaskInstance.query.session.get(AdventurerS1TaskInstance, task_instance_id)
        if not instance:
            return None, '任务不存在'
        if instance.status != 'accepted':
            return None, '只能放弃进行中的任务'

        adv = AdventurerS1.query.session.get(AdventurerS1, instance.adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能放弃自己冒险者的任务'

        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(instance.task_template_id)
        if not template:
            template = templates_map.get(instance.task_template_id, {})
        expedition_task = _is_expedition_task(template)
        building_help_task = _is_building_help_task(template)
        gathering_task = _is_gathering_task(template)
        work_task = _is_work_task(template)
        contribution_task = _is_contribution_task(template)
        treasure_hunt_task = _is_treasure_hunt_task(template)
        hidden_boss_task = _is_hidden_boss_task(template)

        try:
            with db_manager.transaction():
                instance.status = 'abandoned'
                if (
                    not expedition_task
                    and not building_help_task
                    and not gathering_task
                    and not work_task
                    and not contribution_task
                    and not treasure_hunt_task
                    and not hidden_boss_task
                    and adv.status == 'building'
                ):
                    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Svc
                    Svc.enter_rest(adv)
                    adv.building_instance_id = None
                    adv.building_assigned_at = None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

        _invalidate_owner_home_badges(adv)
        return {'id': instance.id, 'status': 'abandoned'}, None

    # ── 完成 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def complete_task(
        task_instance_id: int,
        user_id: Optional[int] = None, is_admin: bool = False,
        auto_commit: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """完成任务：timer 类型检查时长；expedition 类型需手动触发（一般由探险结算自动完成）。"""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        instance = AdventurerS1TaskInstance.query.session.get(AdventurerS1TaskInstance, task_instance_id)
        if not instance:
            return None, '任务不存在'
        if instance.status != 'accepted':
            return None, f'任务状态异常: {instance.status}'

        adv = AdventurerS1.query.session.get(AdventurerS1, instance.adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能完成自己冒险者的任务'

        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(instance.task_template_id)
        if not template:
            template = templates_map.get(instance.task_template_id, {})
        if not template:
            return None, f'任务模板不存在: {instance.task_template_id}'

        if _is_expedition_task(template):
            return None, '探险类任务会在通关指定副本后自动结算'

        if _is_treasure_hunt_task(template):
            return None, '藏宝图会在帮工或采集中随机发现，发现后自动结算'

        if _is_hidden_boss_task(template):
            log = AdventurerS1TaskService._find_qualifying_hidden_boss_log(
                adv.id, instance.started_at,
            )
            if not log and not is_admin:
                return None, '尚未成功击败隐藏Boss'

        if _is_building_help_task(template):
            required = _required_help_seconds(template)
            accumulated = AdventurerS1TaskService._sum_building_help_seconds(
                adv.id, instance.started_at,
            )
            if accumulated < required and not is_admin:
                remaining_min = max(1, int((required - accumulated + 59) / 60))
                return None, f'帮工时长不足，还需约 {remaining_min} 分钟'

        if _is_gathering_task(template):
            required = _required_help_seconds(template)
            accumulated = AdventurerS1TaskService._sum_gathering_seconds(
                adv.id, instance.started_at,
            )
            if accumulated < required and not is_admin:
                remaining_min = max(1, int((required - accumulated + 59) / 60))
                return None, f'采集时长不足，还需约 {remaining_min} 分钟'

        if _is_work_task(template):
            required = _work_required_sessions(template)
            completed_sessions = AdventurerS1TaskService._count_work_sessions(
                adv.id, instance.started_at,
            )
            if completed_sessions < required and not is_admin:
                return None, f'打工次数不足，还需完成 {required - completed_sessions} 次'

        if _is_contribution_task(template):
            required = _required_contribution(template)
            accumulated = AdventurerS1TaskService._sum_contribution_gained(
                adv.id, instance.started_at,
            )
            if accumulated < required and not is_admin:
                return None, f'贡献值不足，还需获得 {required - accumulated} 点'

        duration_minutes = template.get('duration_minutes', 0)
        if (
            duration_minutes > 0
            and not is_admin
            and not _is_building_help_task(template)
            and not _is_gathering_task(template)
            and not _is_work_task(template)
            and not _is_contribution_task(template)
        ):
            elapsed = (_portable_utc_now() - instance.started_at).total_seconds()
            if elapsed < duration_minutes * 60:
                remaining = int((duration_minutes * 60 - elapsed) / 60)
                return None, f'任务尚未完成，还需约 {max(1, remaining)} 分钟'

        return AdventurerS1TaskService._finalize_task(
            instance, adv, template, auto_commit=auto_commit,
        )

    @staticmethod
    def _task_rewards_already_applied(instance_id: int) -> bool:
        """该任务实例是否已有 task_reward 入账（奖励发了但状态未改时的补结依据）。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1CurrencyLog,
            AdventurerS1ItemLog,
            AdventurerS1MaterialLog,
        )

        rid = str(instance_id)
        if AdventurerS1MaterialLog.query.filter_by(
            related_id=rid, related_type='task_instance', source_type='task_reward',
        ).first():
            return True
        if AdventurerS1CurrencyLog.query.filter_by(
            related_id=rid, related_type='task_instance', source_type='task_reward',
        ).first():
            return True
        if AdventurerS1ItemLog.query.filter_by(
            related_id=rid, related_type='task_instance', source_type='task_reward',
        ).first():
            return True
        return False

    @staticmethod
    def _find_qualifying_expedition_log(adventurer_id: int, template: dict, since: datetime):
        """接取任务后，查找满足条件的最近一次通关探险日志。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1ExpeditionLog,
            AdventurerS1ExpeditionLogMember,
        )

        query = (
            AdventurerS1ExpeditionLog.query
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
            )
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.target_dungeon,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.updated_at,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id),
                AdventurerS1ExpeditionLog.status == 'completed',
                # 任务只认接取后实际创建的探险；updated_at 可能因审核、压缩或维护晚于接取时间。
                AdventurerS1ExpeditionLog.created_at >= since,
            )
            .order_by(
                AdventurerS1ExpeditionLog.updated_at.desc(),
                AdventurerS1ExpeditionLog.id.desc(),
            )
        )
        required = template.get('required_dungeon')
        if required:
            query = query.filter(AdventurerS1ExpeditionLog.target_dungeon == required)

        # full_log 可能很大；按 (updated_at, id) 做短事务 keyset 分页，避免流式游标
        # 在整段历史 JSON 校验期间一直保持 PostgreSQL 事务。
        cursor: tuple[datetime, int] | None = None
        while True:
            page_query = query
            if cursor is not None:
                cursor_at, cursor_id = cursor
                page_query = page_query.filter(or_(
                    AdventurerS1ExpeditionLog.updated_at < cursor_at,
                    and_(
                        AdventurerS1ExpeditionLog.updated_at == cursor_at,
                        AdventurerS1ExpeditionLog.id < cursor_id,
                    ),
                ))
            rows = page_query.limit(50).all()
            db_manager.rollback()
            if not rows:
                break
            for log_id, target_dungeon, full_log, updated_at in rows:
                updates = (full_log or {}).get('member_updates', [])
                matched = AdventurerS1TaskService._expedition_satisfies(
                    template, target_dungeon, adventurer_id, updates, log_id,
                )
                db_manager.rollback()
                if matched:
                    return SimpleNamespace(
                        id=int(log_id),
                        target_dungeon=target_dungeon,
                        full_log=full_log,
                        updated_at=updated_at,
                    )
            cursor = (rows[-1].updated_at, int(rows[-1].id))
        return None

    @staticmethod
    def _find_qualifying_hidden_boss_log(adventurer_id: int, since: datetime):
        """接取任务后，查找该冒险者参与且成功击败隐藏Boss的最近探险日志。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1ExpeditionLog,
            AdventurerS1ExpeditionLogMember,
            AdventurerS1HiddenBossState,
        )

        query = (
            AdventurerS1ExpeditionLog.query
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
            )
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.target_dungeon,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.updated_at,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id),
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.updated_at >= since,
            )
            .order_by(
                AdventurerS1ExpeditionLog.updated_at.desc(),
                AdventurerS1ExpeditionLog.id.desc(),
            )
        )
        cursor: tuple[datetime, int] | None = None
        while True:
            page_query = query
            if cursor is not None:
                cursor_at, cursor_id = cursor
                page_query = page_query.filter(or_(
                    AdventurerS1ExpeditionLog.updated_at < cursor_at,
                    and_(
                        AdventurerS1ExpeditionLog.updated_at == cursor_at,
                        AdventurerS1ExpeditionLog.id < cursor_id,
                    ),
                ))
            rows = page_query.limit(50).all()
            if not rows:
                db_manager.rollback()
                break
            boss_rows = (
                AdventurerS1HiddenBossState.query
                .with_entities(
                    AdventurerS1HiddenBossState.dungeon,
                    AdventurerS1HiddenBossState.status,
                    AdventurerS1HiddenBossState.defeated_expedition_log_id,
                )
                .filter(AdventurerS1HiddenBossState.dungeon.in_({row.target_dungeon for row in rows}))
                .all()
            )
            boss_by_dungeon = {row.dungeon: row for row in boss_rows}
            db_manager.rollback()
            for log_id, target_dungeon, full_log, updated_at in rows:
                boss_state = boss_by_dungeon.get(target_dungeon)
                if (
                    boss_state
                    and boss_state.status == 'defeated'
                    and boss_state.defeated_expedition_log_id == log_id
                    and _is_task_hidden_boss_victory_log(full_log)
                ):
                    return SimpleNamespace(
                        id=int(log_id),
                        target_dungeon=target_dungeon,
                        full_log=full_log,
                        updated_at=updated_at,
                    )
            cursor = (rows[-1].updated_at, int(rows[-1].id))
        return None

    @staticmethod
    def reconcile_stale_hidden_boss_task(instance, template: dict) -> Tuple[bool, Optional[str]]:
        """按已落库的成功击杀日志补结算隐藏 Boss 委托。"""
        from plugins.adventurer_s1.models import AdventurerS1

        if instance.status != 'accepted' or not _is_hidden_boss_task(template):
            return False, None
        log = AdventurerS1TaskService._find_qualifying_hidden_boss_log(
            instance.adventurer_id,
            instance.started_at,
        )
        if not log:
            return False, None
        adv = AdventurerS1.query.session.get(AdventurerS1, instance.adventurer_id)
        if not adv:
            return False, '冒险者不存在'
        if AdventurerS1TaskService._task_rewards_already_applied(instance.id):
            _, err = AdventurerS1TaskService._mark_task_completed(instance, adv, template)
        else:
            _, err = AdventurerS1TaskService._finalize_task(instance, adv, template)
        return err is None, err

    @staticmethod
    def reconcile_stale_expedition_task(instance) -> Tuple[bool, Optional[str]]:
        """探险任务仍 accepted 但已有匹配通关记录时补结算（防 settle 漏跑或状态未落库）。

        若奖励已入账则只改状态，避免重复发奖。
        返回 (是否已 reconcile, error)。
        """
        from plugins.adventurer_s1.models import AdventurerS1

        if instance.status != 'accepted':
            return False, None

        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(instance.task_template_id)
        if not template:
            base_template = templates_map.get(instance.task_template_id)
            if not base_template or not _is_expedition_task(base_template):
                return False, None
            template, _ = _resolve_effective_template(base_template)
        elif not _is_expedition_task(template):
            return False, None

        log = AdventurerS1TaskService._find_qualifying_expedition_log(
            instance.adventurer_id, template, instance.started_at,
        )
        if not log:
            return False, None

        adv = AdventurerS1.query.session.get(AdventurerS1, instance.adventurer_id)
        if not adv or adv.status == 'dead':
            return False, None

        if AdventurerS1TaskService._task_rewards_already_applied(instance.id):
            _, err = AdventurerS1TaskService._mark_task_completed(instance, adv, template)
            return (err is None), err

        _, err = AdventurerS1TaskService._finalize_task(instance, adv, template)
        return (err is None), err

    @staticmethod
    def _mark_task_completed(instance, adv, template: dict) -> Tuple[Optional[dict], Optional[str]]:
        """仅将任务标为 completed（奖励已发过，不再重复入账）。"""
        try:
            with db_manager.transaction():
                locked = AdventurerS1TaskInstance.query.session.get(AdventurerS1TaskInstance, instance.id, with_for_update=True)
                if not locked:
                    raise S1PlayerVisibleError('任务不存在')
                if locked.status != 'accepted':
                    return None, None
                locked.status = 'completed'
                locked.completed_at = _portable_utc_now()
                instance = locked
        except RuntimeError as e:
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            return None, player_operation_failure(e, '结算失败')

        AdventurerS1TaskService._emit_task_complete_socket(adv.id)
        return instance.to_dict(
            template=AdventurerS1TaskService._template_for_client(template),
        ), None

    @staticmethod
    def _emit_task_board_changed(
        reason: str = 'updated',
        task_id: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> None:
        """全服广播：任务栏配置变更（新委托/重新发布等），前端刷新可接数并提示玩家。"""
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            payload = {'reason': reason}
            if task_id:
                payload['task_id'] = task_id
            if task_name:
                payload['task_name'] = task_name
            socketio.emit('task_board_changed', payload, namespace='/')
        except Exception:
            pass

    @staticmethod
    def _emit_task_complete_socket(adventurer_id: int) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': 'task_complete', 'reason': 'task_settle',
                'ids': [adventurer_id],
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def _finalize_task(
        instance,
        adv,
        template: dict,
        defer_hidden_boss_end: bool = False,
        auto_commit: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """标记完成、发奖励、释放 timer 类型占用的冒险者。"""
        rewards = template.get('rewards') or {}
        expedition_task = _is_expedition_task(template)
        building_help_task = _is_building_help_task(template)
        gathering_task = _is_gathering_task(template)
        work_task = _is_work_task(template)
        contribution_task = _is_contribution_task(template)
        treasure_hunt_task = _is_treasure_hunt_task(template)
        hidden_boss_task = _is_hidden_boss_task(template)
        max_comp = _task_max_completions(template)
        quota_full = False
        try:
            with (db_manager.transaction() if auto_commit else nullcontext()):
                locked = AdventurerS1TaskInstance.query.session.get(AdventurerS1TaskInstance, instance.id, with_for_update=True)
                if not locked:
                    return None, '任务不存在'
                if locked.status != 'accepted':
                    return None, f'任务状态异常: {locked.status}'

                if max_comp and not hidden_boss_task:
                    from plugins.adventurer_s1.models import AdventurerS1TaskInstance as TaskInst
                    TaskInst.query.filter(
                        TaskInst.task_template_id == locked.task_template_id,
                        TaskInst.status.in_(('accepted', 'completed')),
                    ).with_for_update().all()
                    completed = AdventurerS1TaskService._count_completions(locked.task_template_id)
                    if completed >= max_comp:
                        quota_full = True
                    else:
                        AdventurerS1TaskService._apply_rewards(adv, rewards, locked)
                        locked.status = 'completed'
                        locked.completed_at = _portable_utc_now()
                        instance = locked
                else:
                    AdventurerS1TaskService._apply_rewards(adv, rewards, locked)
                    locked.status = 'completed'
                    locked.completed_at = _portable_utc_now()
                    instance = locked

                if (
                    not quota_full
                    and not expedition_task
                    and not building_help_task
                    and not gathering_task
                    and not work_task
                    and not contribution_task
                    and not treasure_hunt_task
                    and not hidden_boss_task
                ):
                    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service as Svc
                    Svc.enter_rest(adv)
                    adv.building_instance_id = None
                    adv.building_assigned_at = None
        except Exception as e:
            return None, player_operation_failure(e, '结算失败')

        if quota_full:
            return None, '任务名额已满'

        if treasure_hunt_task and max_comp:
            completed_after = AdventurerS1TaskService._count_completions(instance.task_template_id)
            if completed_after >= max_comp:
                AdventurerS1TaskService.expire_remaining_accepted_tasks(instance.task_template_id)
                AdventurerS1TaskService._emit_task_board_changed(
                    reason='treasure_hunt_ended',
                    task_id=instance.task_template_id,
                    task_name=template.get('name'),
                )

        if (
            hidden_boss_task
            and not defer_hidden_boss_end
            and AdventurerS1TaskService.is_hidden_boss_ended()
        ):
            AdventurerS1TaskService.expire_nonclaimable_hidden_boss_tasks(instance.task_template_id)
            AdventurerS1TaskService._emit_task_board_changed(
                reason='hidden_boss_ended',
                task_id=instance.task_template_id,
                task_name=template.get('name'),
            )

        AdventurerS1TaskService._emit_task_complete_socket(adv.id)
        if treasure_hunt_task:
            dropped_item = None
            for entry in (rewards.get('items') or []):
                if isinstance(entry, dict):
                    dropped_item = entry.get('name') or entry.get('item_name')
                elif isinstance(entry, str):
                    dropped_item = entry
                if dropped_item:
                    break
            try:
                from plugins.adventurer_s1.services.s1_socket_service import (
                    deployment_s1_realm, emit_s1_to_user,
                )
                payload = {
                    'action': 'treasure_map_found',
                    'reason': 'treasure_hunt_drop',
                    'ids': [adv.id],
                }
                if dropped_item:
                    payload['dropped_item'] = str(dropped_item)
                emit_s1_to_user(deployment_s1_realm(), adv.owner_id, 'adventurer_changed', payload)
            except Exception:
                pass
        _invalidate_owner_home_badges(adv)
        return instance.to_dict(
            template=AdventurerS1TaskService._template_for_client(template),
        ), None

    # ── 藏宝图搜寻（treasure_hunt）────────────────────────────────────────────

    @staticmethod
    def treasure_hunt_assignments(
        source_template_id: str, adventurer_ids: list[int],
    ) -> dict[int, str]:
        """批量返回冒险者实际接取的藏宝图任务版本 ID。

        任务栏“发布新版本”会为同一根模板生成 ``__rN`` ID。掉落触发必须按
        实例中的真实版本结算，不能继续只查询根模板 ID。
        """
        if not adventurer_ids:
            return {}
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance

        override_map = _preload_all_overrides()
        family_ids = {
            task_id
            for task_id, template in _runtime_templates_map(
                include_disabled=True,
                override_map=override_map,
            ).items()
            if _is_treasure_hunt_task(template)
            and (
                task_id == source_template_id
                or (
                    override_map.get(task_id) is not None
                    and override_map[task_id].source_template_id == source_template_id
                )
            )
        }
        if not family_ids:
            return {}

        rows = AdventurerS1TaskInstance.query.filter(
            AdventurerS1TaskInstance.adventurer_id.in_(adventurer_ids),
            AdventurerS1TaskInstance.task_template_id.in_(family_ids),
            AdventurerS1TaskInstance.status == 'accepted',
        ).with_entities(
            AdventurerS1TaskInstance.adventurer_id,
            AdventurerS1TaskInstance.task_template_id,
        ).all()
        return {int(row[0]): str(row[1]) for row in rows}

    @staticmethod
    def is_treasure_hunt_ended(
        task_template_id: str, override_map: dict = None, stats_map: dict = None,
    ) -> bool:
        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(task_template_id, override_map=override_map)
        if not template:
            template = templates_map.get(task_template_id)
        if not template or not _is_treasure_hunt_task(template):
            return False
        return AdventurerS1TaskService._is_task_ended(template, stats_map=stats_map)

    @staticmethod
    def filter_treasure_hunt_adventurer_ids(
        task_template_id: str, adventurer_ids: list[int],
    ) -> list[int]:
        """返回已接取指定 treasure_hunt 且仍进行中的冒险者 ID。"""
        if not adventurer_ids:
            return []
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance

        rows = AdventurerS1TaskInstance.query.filter(
            AdventurerS1TaskInstance.adventurer_id.in_(adventurer_ids),
            AdventurerS1TaskInstance.task_template_id == task_template_id,
            AdventurerS1TaskInstance.status == 'accepted',
        ).with_entities(AdventurerS1TaskInstance.adventurer_id).all()
        return [row[0] for row in rows]

    @staticmethod
    def _treasure_hunt_drop_weight_for_rarity(rarity: str | None) -> int:
        from flask import current_app

        r = str(rarity or '').strip().lower()
        if r == 'legendary':
            raw = current_app.config.get('ADVENTURER_S1_TREASURE_HUNT_LEGENDARY_DROP_WEIGHT', 1)
        elif r == 'epic':
            raw = current_app.config.get('ADVENTURER_S1_TREASURE_HUNT_EPIC_DROP_WEIGHT', 2)
        else:
            return 1
        try:
            weight = int(raw)
        except (TypeError, ValueError):
            weight = 1 if r == 'legendary' else 2
        return max(1, weight)

    @staticmethod
    def _pick_treasure_hunt_drop_item(pool: list[str]) -> str | None:
        """按图鉴稀有度加权抽 1 种门票（史诗权重默认为传说的 2 倍）。"""
        import random

        from plugins.adventurer_s1.models import AdventurerS1Item

        names = [str(n or '').strip() for n in pool if str(n or '').strip()]
        if not names:
            return None
        unique = list(dict.fromkeys(names))
        if len(unique) == 1:
            return unique[0]

        rarity_by_name = {
            row.name: row.rarity
            for row in AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(unique)).all()
        }
        weights = [
            AdventurerS1TaskService._treasure_hunt_drop_weight_for_rarity(rarity_by_name.get(name))
            for name in unique
        ]
        if sum(weights) <= 0:
            return random.choice(unique)
        return random.choices(unique, weights=weights, k=1)[0]

    @staticmethod
    def _treasure_hunt_drop_rewards(rewards: dict | None) -> dict:
        """机缘门票掉落：每次只加权随机发放奖池中的一张门票（quantity=1）。"""
        base = dict(rewards or {})
        pool: list[str] = []
        for entry in base.get('items') or []:
            if isinstance(entry, str):
                name = entry.strip()
            elif isinstance(entry, dict):
                name = str(entry.get('name') or entry.get('item_name') or '').strip()
            else:
                continue
            if name:
                pool.append(name)
        if not pool:
            return base
        picked = AdventurerS1TaskService._pick_treasure_hunt_drop_item(pool)
        if not picked:
            return base
        out = {k: v for k, v in base.items() if k != 'items'}
        out['items'] = [{'name': picked, 'quantity': 1}]
        return out

    @staticmethod
    def finalize_treasure_hunt_drop(
        adventurer_id: int, task_template_id: str,
    ) -> Tuple[bool, Optional[str]]:
        """随机掉落命中后结算：发奖、完成任务；满额时作废其余进行中的接取。"""
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1TaskInstance

        if AdventurerS1TaskService.is_treasure_hunt_ended(task_template_id):
            AdventurerS1TaskService.expire_remaining_accepted_tasks(task_template_id)
            return False, '任务已结束'

        instance = AdventurerS1TaskInstance.query.filter_by(
            adventurer_id=adventurer_id,
            task_template_id=task_template_id,
            status='accepted',
        ).first()
        if not instance:
            return False, None

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv or adv.status == 'dead':
            return False, '冒险者不可用'

        _, templates_map = _load_templates()
        template, _ = _resolve_effective_template_by_id(task_template_id)
        if not template:
            template = templates_map.get(task_template_id, {})
        if not template:
            return False, '任务模板不存在'

        settle_template = dict(template)
        settle_template['rewards'] = AdventurerS1TaskService._treasure_hunt_drop_rewards(
            template.get('rewards'),
        )
        _, err = AdventurerS1TaskService._finalize_task(instance, adv, settle_template)
        if err:
            return False, err
        return True, None

    @staticmethod
    def expire_remaining_accepted_tasks(task_template_id: str, reason: str = 'treasure_hunt_ended') -> int:
        """全服完成型任务结束后，将仍进行中的接取标记为 expired。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance

        stale = AdventurerS1TaskInstance.query.filter_by(
            task_template_id=task_template_id,
            status='accepted',
        ).all()
        if not stale:
            return 0

        expired_adv_ids = [row.adventurer_id for row in stale]
        try:
            with db_manager.transaction():
                updated = AdventurerS1TaskInstance.query.filter(
                    AdventurerS1TaskInstance.task_template_id == task_template_id,
                    AdventurerS1TaskInstance.status == 'accepted',
                ).update({'status': 'expired'}, synchronize_session=False)
        except Exception as e:
            logger.warning('作废藏宝图任务失败 task=%s err=%s', task_template_id, e)
            return 0

        if updated:
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {
                    'action': 'task_expired',
                    'reason': reason,
                    'ids': expired_adv_ids,
                    'task_template_id': task_template_id,
                }, namespace='/')
            except Exception:
                pass
        return int(updated or 0)

    @staticmethod
    def expire_nonclaimable_hidden_boss_tasks(task_template_id: str) -> int:
        """隐藏 Boss 全部结束后，分页作废没有成功击杀记录的进行中委托。"""
        from plugins.adventurer_s1.models import (
            AdventurerS1ExpeditionLog,
            AdventurerS1ExpeditionLogMember,
            AdventurerS1TaskInstance,
        )
        batch_size = 500
        last_id = 0
        total_updated = 0
        while True:
            accepted = AdventurerS1TaskInstance.query.filter(
                AdventurerS1TaskInstance.task_template_id == task_template_id,
                AdventurerS1TaskInstance.status == 'accepted',
                AdventurerS1TaskInstance.id > last_id,
            ).with_entities(
                AdventurerS1TaskInstance.id,
                AdventurerS1TaskInstance.adventurer_id,
                AdventurerS1TaskInstance.started_at,
            ).order_by(AdventurerS1TaskInstance.id.asc()).limit(batch_size).all()
            if not accepted:
                break
            last_id = accepted[-1].id

            started_at_by_adv = {row.adventurer_id: row.started_at for row in accepted}
            qualifying_adv_ids: set[int] = set()
            logs_query = (
                AdventurerS1ExpeditionLog.query
                .join(
                    AdventurerS1ExpeditionLogMember,
                    AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
                )
                .filter(
                    AdventurerS1ExpeditionLogMember.adventurer_id.in_(started_at_by_adv),
                    AdventurerS1ExpeditionLog.status == 'completed',
                    AdventurerS1ExpeditionLog.updated_at >= min(started_at_by_adv.values()),
                )
                .with_entities(
                    AdventurerS1ExpeditionLogMember.adventurer_id,
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.updated_at,
                    AdventurerS1ExpeditionLog.full_log,
                )
            )
            log_cursor: tuple[int, int] | None = None
            while True:
                page_query = logs_query
                if log_cursor is not None:
                    cursor_log_id, cursor_adv_id = log_cursor
                    page_query = page_query.filter(or_(
                        AdventurerS1ExpeditionLog.id > cursor_log_id,
                        and_(
                            AdventurerS1ExpeditionLog.id == cursor_log_id,
                            AdventurerS1ExpeditionLogMember.adventurer_id > cursor_adv_id,
                        ),
                    ))
                log_rows = page_query.order_by(
                    AdventurerS1ExpeditionLog.id.asc(),
                    AdventurerS1ExpeditionLogMember.adventurer_id.asc(),
                ).limit(100).all()
                db_manager.rollback()
                if not log_rows:
                    break
                for adventurer_id, _log_id, updated_at, full_log in log_rows:
                    if adventurer_id in qualifying_adv_ids:
                        continue
                    if updated_at < started_at_by_adv[adventurer_id]:
                        continue
                    if _is_task_hidden_boss_victory_log(full_log):
                        qualifying_adv_ids.add(adventurer_id)
                log_cursor = (int(log_rows[-1].id), int(log_rows[-1].adventurer_id))

            expired = [row for row in accepted if row.adventurer_id not in qualifying_adv_ids]
            if not expired:
                continue
            expired_ids = [row.id for row in expired]
            expired_adv_ids = [row.adventurer_id for row in expired]
            try:
                with db_manager.transaction():
                    updated = AdventurerS1TaskInstance.query.filter(
                        AdventurerS1TaskInstance.id.in_(expired_ids),
                        AdventurerS1TaskInstance.status == 'accepted',
                    ).update({'status': 'expired'}, synchronize_session=False)
            except Exception as e:
                logger.warning('作废隐藏Boss委托失败 task=%s err=%s', task_template_id, e)
                continue
            total_updated += int(updated or 0)
            if updated:
                try:
                    from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                    socketio.emit('adventurer_changed', {
                        'action': 'task_expired',
                        'reason': 'hidden_boss_ended',
                        'ids': expired_adv_ids,
                        'task_template_id': task_template_id,
                    }, namespace='/')
                except Exception:
                    pass
        return total_updated

    @staticmethod
    def settle_contribution_task(adventurer_id: int, auto_commit: bool = True) -> bool:
        """贡献入账后自动结算已达标的进行中贡献任务。"""
        instance = (
            AdventurerS1TaskInstance.query
            .filter_by(adventurer_id=int(adventurer_id), status='accepted')
            .order_by(AdventurerS1TaskInstance.id.desc())
            .first()
        )
        if not instance:
            return False

        template, _ = _resolve_effective_template_by_id(instance.task_template_id)
        if not template:
            _, templates_map = _load_templates()
            template = templates_map.get(instance.task_template_id)
        if not template or not _is_contribution_task(template):
            return False
        if AdventurerS1TaskService._sum_contribution_gained(
            int(adventurer_id), instance.started_at,
        ) < _required_contribution(template):
            return False

        result, error = AdventurerS1TaskService.complete_task(
            instance.id, auto_commit=auto_commit,
        )
        if error:
            logger.warning(
                '自动结算贡献任务失败 task_instance_id=%s err=%s', instance.id, error,
            )
            return False
        return result is not None

    @staticmethod
    def settle_expedition_tasks(
        adventurer_ids: list,
        target_dungeon: str,
        member_updates: list | None = None,
        result_data: dict | None = None,
        expedition_log_id: int | None = None,
    ) -> list[int]:
        """探险结算后，为匹配的探险及隐藏 Boss 任务自动发奖。"""
        if not adventurer_ids or not target_dungeon:
            return []

        from plugins.adventurer_s1.models import (
            AdventurerS1,
            AdventurerS1ExpeditionLog,
            AdventurerS1HiddenBossState,
            AdventurerS1TaskInstance,
        )

        settled: list[int] = []
        updates = member_updates or []
        expedition_created_at = None
        if expedition_log_id:
            created_row = db_manager.query(AdventurerS1ExpeditionLog.created_at).filter(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
            ).first()
            expedition_created_at = created_row.created_at if created_row else None

        # 以 boss 状态表为准：只有 boss 确实被本次探险击败才结算隐藏 Boss 任务
        hidden_boss_completed = False
        if expedition_log_id:
            boss_state = AdventurerS1HiddenBossState.query.filter_by(
                dungeon=target_dungeon,
            ).first()
            if (
                boss_state
                and boss_state.status == 'defeated'
                and boss_state.defeated_expedition_log_id == expedition_log_id
                and _is_task_hidden_boss_victory_log(result_data)
            ):
                hidden_boss_completed = True
                from plugins.adventurer_s1.services.s1_hidden_boss_service import S1HiddenBossService
                S1HiddenBossService.record_task_boss_defeated(target_dungeon)

        instances = AdventurerS1TaskInstance.query.filter(
            AdventurerS1TaskInstance.adventurer_id.in_(adventurer_ids),
            AdventurerS1TaskInstance.status == 'accepted',
        ).all()

        adventurers = AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids)).all()
        adventurer_map = {adv.id: adv for adv in adventurers}
        hidden_boss_task_ids: set[str] = set()

        for instance in instances:
            template, _ = _resolve_effective_template_by_id(instance.task_template_id)
            if not template:
                continue
            hidden_boss_task = _is_hidden_boss_task(template)
            if hidden_boss_task:
                if not hidden_boss_completed:
                    continue
                hidden_boss_task_ids.add(instance.task_template_id)
            else:
                if not _is_expedition_task(template):
                    continue
                if (
                    expedition_created_at is not None
                    and instance.started_at is not None
                    and expedition_created_at < instance.started_at
                ):
                    continue
                if not AdventurerS1TaskService._expedition_satisfies(
                    template, target_dungeon, instance.adventurer_id, updates, expedition_log_id,
                ):
                    continue

            adv = adventurer_map.get(instance.adventurer_id)
            if not adv or (not hidden_boss_task and adv.status == 'dead'):
                continue

            _, err = AdventurerS1TaskService._finalize_task(
                instance,
                adv,
                template,
                defer_hidden_boss_end=hidden_boss_task,
            )
            if not err:
                settled.append(adv.id)
            else:
                logger.warning(
                    '探险任务自动结算失败 adv=%s task=%s err=%s',
                    adv.id, instance.task_template_id, err,
                )

        if hidden_boss_completed and AdventurerS1TaskService.is_hidden_boss_ended():
            hidden_boss_task_ids.update(
                task_id
                for task_id, template in _runtime_templates_map(include_disabled=True).items()
                if _is_hidden_boss_task(template)
            )
            for task_template_id in hidden_boss_task_ids:
                AdventurerS1TaskService.expire_nonclaimable_hidden_boss_tasks(task_template_id)
                template, _ = _resolve_effective_template_by_id(task_template_id)
                AdventurerS1TaskService._emit_task_board_changed(
                    reason='hidden_boss_ended',
                    task_id=task_template_id,
                    task_name=(template or {}).get('name'),
                )
        return settled

    # ── 奖励应用 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_rewards(adv, rewards: dict, instance=None):
        """应用任务奖励，复用 BuildingSettlementService 的奖励逻辑。"""
        related_id = instance.id if instance else None

        if rewards.get('gold', 0) > 0:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            _, gold_err = S1CurrencyService.add(
                adv.id, 'gold', float(rewards['gold']),
                source_type='task_reward',
                related_id=related_id,
                related_type='task_instance',
                description='任务奖励',
                auto_commit=False,
            )
            if gold_err:
                raise RuntimeError(f'任务金币入账失败: {gold_err}')

        if rewards.get('exp', 0) > 0:
            adv.exp = int((adv.exp or 0) + rewards['exp'])

        if rewards.get('stat'):
            from plugins.adventurer_s1.services.s1_adventurer_stat_service import (
                S1_BASE_ATTRS, add_s1_attr, add_s1_max_hp, clamp_s1_adventurer_stats,
            )
            for stat_name, amount in rewards['stat'].items():
                if stat_name in S1_BASE_ATTRS:
                    add_s1_attr(adv, stat_name, int(amount))
                elif stat_name == 'max_hp' and hasattr(adv, 'max_hp'):
                    add_s1_max_hp(adv, int(amount), sync_hp=True)
                elif hasattr(adv, stat_name):
                    current = int(getattr(adv, stat_name) or 0)
                    setattr(adv, stat_name, current + int(amount))
            clamp_s1_adventurer_stats(adv)

        if rewards.get('materials'):
            from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
            for mat_name, amount in rewards['materials'].items():
                S1MaterialService.add(
                    adv.id, mat_name, int(amount),
                    source_type='task_reward',
                    related_id=related_id,
                    related_type='task_instance',
                    description=f'任务奖励: {mat_name}',
                    auto_commit=False,
                )

        if rewards.get('items'):
            from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
            for entry in rewards['items']:
                if isinstance(entry, str):
                    item_name, quantity = entry, 1
                elif isinstance(entry, dict):
                    item_name = entry.get('name') or entry.get('item_name')
                    quantity = int(entry.get('quantity', 1) or 1)
                else:
                    continue
                if not item_name:
                    continue
                _, item_err = grant_catalog_item(
                    adv,
                    str(item_name),
                    quantity=quantity,
                    prefer_warehouse=True,
                    source_type='task_reward',
                    related_id=str(related_id) if related_id is not None else None,
                    related_type='task_instance' if related_id is not None else None,
                    description=f'任务奖励: {item_name}',
                )
                if item_err:
                    raise RuntimeError(f'任务物品奖励发放失败: {item_err}')

    # ── 管理员：任务栏运营配置 ────────────────────────────────────────────────

    @staticmethod
    def _admin_task_stats(task_template_id: str, template: dict) -> dict:
        max_comp = _task_max_completions(template)
        if _is_hidden_boss_task(template):
            progress = AdventurerS1TaskService.hidden_boss_progress()
            completed = progress['defeated']
            occupied = AdventurerS1TaskService._count_occupied_slots(task_template_id) if max_comp else None
            remaining = progress['remaining']
            is_ended = progress['ended']
        else:
            completed = AdventurerS1TaskService._count_completions(task_template_id) if max_comp else None
            occupied = AdventurerS1TaskService._count_occupied_slots(task_template_id) if max_comp else None
            remaining = max(0, max_comp - occupied) if max_comp else None
            is_ended = bool(max_comp and completed is not None and completed >= max_comp)
        return {
            'max_completions': max_comp,
            'completion_count': completed,
            'completion_remaining': remaining,
            'occupied_count': occupied,
            'is_ended': is_ended,
        }

    @staticmethod
    def _admin_build_task_row(task_id: str, effective: dict, override=None) -> dict:
        stats = AdventurerS1TaskService._admin_task_stats(task_id, effective)
        _, json_map = _load_templates()
        base = json_map.get(_runtime_source_id(task_id) or task_id)
        return {
            'id': task_id,
            'source_template_id': override.source_template_id if override else None,
            'is_edition': bool(override and override.source_template_id),
            'completion_type': _completion_type(effective),
            'enabled': _is_task_enabled(task_id, override),
            'override': override.overrides_json if override and override.overrides_json else {},
            'template': {
                'name': effective.get('name'),
                'description': effective.get('description'),
                'icon': effective.get('icon'),
                'duration_minutes': effective.get('duration_minutes', 0),
                'required_sessions': effective.get('required_sessions'),
                'required_dungeon': effective.get('required_dungeon'),
                'no_damage': bool(effective.get('no_damage')),
                'repeatable': bool(effective.get('repeatable')),
                'max_completions': stats['max_completions'],
                'rewards': effective.get('rewards') or {},
                'narrative': effective.get('narrative'),
                'requirements': effective.get('requirements') or {},
            },
            'base_template': {
                'name': (base or effective).get('name'),
                'max_completions': _task_max_completions(base) if base else stats['max_completions'],
                'rewards': (base or effective).get('rewards') or {},
            },
            **stats,
        }

    @staticmethod
    def admin_list_tasks() -> Tuple[Optional[list], Optional[str]]:
        """管理员查看全部任务模板（含停用/已结束/派生新版本）及覆盖配置。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskOverride

        templates, _ = _load_templates()
        result = []
        seen: set[str] = set()

        for base in templates:
            task_id = base['id']
            effective, override = _resolve_effective_template(base)
            result.append(AdventurerS1TaskService._admin_build_task_row(task_id, effective, override))
            seen.add(task_id)

        for clone in AdventurerS1TaskOverride.query.filter(
            AdventurerS1TaskOverride.source_template_id.isnot(None),
        ).order_by(AdventurerS1TaskOverride.task_template_id).all():
            if clone.task_template_id in seen:
                continue
            effective, _ = _resolve_effective_template_by_id(clone.task_template_id)
            if not effective:
                continue
            result.append(AdventurerS1TaskService._admin_build_task_row(
                clone.task_template_id, effective, clone,
            ))
            seen.add(clone.task_template_id)

        return result, None

    @staticmethod
    def _sanitize_admin_overrides(payload: dict) -> dict:
        cleaned = {}
        for key, value in (payload or {}).items():
            if key not in ADMIN_EDITABLE_TEMPLATE_FIELDS:
                continue
            if key == 'max_completions':
                if value is None or value == '':
                    continue
                cleaned[key] = max(1, int(value))
            elif key in ('duration_minutes', 'required_sessions', 'required_contribution'):
                if value is None or value == '':
                    continue
                cleaned[key] = max(0, int(value))
            elif key in ('no_damage', 'required_random_solo_match', 'repeatable'):
                cleaned[key] = bool(value)
            elif key == 'rewards' and isinstance(value, dict):
                rewards = {}
                if value.get('gold') is not None and value.get('gold') != '':
                    rewards['gold'] = max(0, int(value['gold']))
                if value.get('exp') is not None and value.get('exp') != '':
                    rewards['exp'] = max(0, int(value['exp']))
                if value.get('materials') and isinstance(value['materials'], dict):
                    rewards['materials'] = {
                        str(k): max(0, int(v)) for k, v in value['materials'].items()
                    }
                if rewards:
                    cleaned['rewards'] = rewards
            elif key == 'requirements' and isinstance(value, dict):
                cleaned['requirements'] = value
            elif value is not None and value != '':
                cleaned[key] = value
        return cleaned

    @staticmethod
    def admin_update_task(
        task_template_id: str,
        payload: dict,
        user_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """更新任务启用状态与可运营字段。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskOverride

        effective, existing_override = _resolve_effective_template_by_id(task_template_id)
        if not effective:
            return None, f'任务不存在: {task_template_id}'

        enabled = payload.get('enabled')
        overrides_payload = payload.get('overrides')
        if enabled is None and overrides_payload is None:
            return None, '缺少可更新字段'

        override = existing_override or AdventurerS1TaskOverride.query.session.get(AdventurerS1TaskOverride, task_template_id)
        if not override:
            override = AdventurerS1TaskOverride(task_template_id=task_template_id)
            if enabled is None:
                override.enabled = _is_task_enabled(task_template_id)
            db_manager.add(override)

        if enabled is not None:
            was_enabled = bool(override.enabled) if existing_override else False
            override.enabled = bool(enabled)
            # 隐藏Boss任务从关→开时，清空 boss 状态归零
            if _runtime_source_id(task_template_id) == _HIDDEN_BOSS_TASK_ID and not was_enabled and override.enabled:
                from plugins.adventurer_s1.models import AdventurerS1HiddenBossState
                AdventurerS1HiddenBossState.query.delete()

        if overrides_payload is not None:
            current = dict(override.overrides_json or {})
            current.update(AdventurerS1TaskService._sanitize_admin_overrides(overrides_payload))
            override.overrides_json = current or None

        override.updated_by_user_id = user_id
        ok, err = db_manager.commit()
        if not ok:
            return None, err or '保存失败'
        _invalidate_overrides_cache()

        effective, override = _resolve_effective_template_by_id(task_template_id)
        row = AdventurerS1TaskService._admin_build_task_row(task_template_id, effective, override)
        AdventurerS1TaskService._emit_task_board_changed(
            reason='updated',
            task_id=task_template_id,
            task_name=(effective or {}).get('name'),
        )
        return row, None

    @staticmethod
    def admin_reenable_task(
        task_template_id: str,
        user_id: Optional[int] = None,
        max_completions: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """重新启用：停用当前任务并发布带新 ID 的新版本（名额从零计）。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskOverride

        effective, current_override = _resolve_effective_template_by_id(task_template_id)
        if not effective:
            return None, f'任务不存在: {task_template_id}'

        source_id = _runtime_source_id(task_template_id)
        if not source_id:
            return None, f'无法派生新任务: {task_template_id}'

        if max_completions is not None:
            effective['max_completions'] = max(1, int(max_completions))

        new_task_id = _next_edition_task_id(source_id)
        snapshot = _snapshot_editable_fields(effective)

        try:
            with db_manager.transaction():
                old_override = current_override or AdventurerS1TaskOverride(task_template_id=task_template_id)
                if not current_override:
                    db_manager.add(old_override)
                old_override.enabled = False
                old_override.updated_by_user_id = user_id

                new_override = AdventurerS1TaskOverride(
                    task_template_id=new_task_id,
                    source_template_id=source_id,
                    enabled=True,
                    overrides_json=snapshot,
                    updated_by_user_id=user_id,
                )
                db_manager.add(new_override)
        except Exception as e:
            return None, f'重新启用失败: {e}'
        _invalidate_overrides_cache()

        new_effective, new_override = _resolve_effective_template_by_id(new_task_id)
        row = AdventurerS1TaskService._admin_build_task_row(new_task_id, new_effective, new_override)
        row['disabled_task_id'] = task_template_id
        AdventurerS1TaskService._emit_task_board_changed(
            reason='new_edition',
            task_id=new_task_id,
            task_name=new_effective.get('name'),
        )
        return row, None


def enable_hidden_boss_task() -> bool:
    """GM 手动开启隐藏Boss讨伐委托。开启时清空所有 boss 状态，进度归零。幂等安全。"""
    from plugins.adventurer_s1.models import AdventurerS1TaskOverride, AdventurerS1HiddenBossState

    override = AdventurerS1TaskOverride.query.filter_by(
        task_template_id=_HIDDEN_BOSS_TASK_ID,
    ).first()
    if override:
        if override.enabled:
            return False  # 已经是启用状态，无需改动
        override.enabled = True
        override.updated_at = _portable_utc_now()
    else:
        db_manager.add(AdventurerS1TaskOverride(
            task_template_id=_HIDDEN_BOSS_TASK_ID,
            enabled=True,
        ))
    # 清空所有 boss 状态与任务栏独立进度，进度归零
    AdventurerS1HiddenBossState.query.delete()
    from plugins.adventurer_s1.services.s1_hidden_boss_service import S1HiddenBossService
    S1HiddenBossService.clear_task_defeated_dungeons()
    ok, err = db_manager.commit()
    if not ok:
        logger.error('启用隐藏Boss任务失败: %s', err)
        return False
    _invalidate_overrides_cache()
    logger.info('隐藏Boss讨伐委托已开启，boss状态已重置')
    return True


from plugins.adventurer_s1.models import AdventurerS1TaskInstance
