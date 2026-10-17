"""S1 建筑分级升级配置读取工具。"""

from typing import Any, Mapping


def building_max_level(upgrade_config: Mapping[str, Any] | None) -> int:
    """返回建筑配置允许的最高等级；旧版单级配置默认最高 Lv.2。"""
    if not upgrade_config:
        return 1
    try:
        return max(2, int(upgrade_config.get('max_level', 2)))
    except (TypeError, ValueError):
        return 2


def building_upgrade_stage_config(
    upgrade_config: Mapping[str, Any] | None,
    current_level: int,
) -> dict[str, Any]:
    """读取从 current_level 升到下一级的配置，兼容旧版根级 Lv.2 配置。"""
    if not upgrade_config:
        return {}

    target_level = max(1, int(current_level or 1)) + 1
    if target_level > building_max_level(upgrade_config):
        return {}

    levels = upgrade_config.get('levels') or {}
    stage = levels.get(str(target_level)) or levels.get(target_level)
    if isinstance(stage, Mapping):
        return dict(stage)
    return dict(upgrade_config) if target_level == 2 else {}
