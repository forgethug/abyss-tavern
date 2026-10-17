"""建筑玩家 API 下发前的字段裁剪（F12 不可见内部配置）。"""
from __future__ import annotations

# 玩家 UI / 玩法决策需要的 settlement_config 键（白名单）
_PLAYER_SETTLEMENT_CONFIG_KEYS = frozenset({
    'description',
    'gold_cost',
    'gold_cost_per_hour',
    'gold_cost_per_hp',
    'gold_cost_per_minute',
    'usage_stamina_cost',
    'usage_duration_minutes',
    'usage_duration_minutes_per_hp',
    'cooldown_hours',
    'daily_limit',
    'max_search_hours',
    'stamina_cost_per_hour',
    'unlimited_usage',
    'required_stat',
    'required_stat_min',
    'usage_slots',
    'reward_type',
    'reward_amount',
    'reward_material_name',
    'reward_currency_code',
    'input_materials',
    'hp_percent_per_minute',
    'stat_name',
    'rest_like_recovery',
})


def sanitize_settlement_config_for_client(
    config: dict | None,
    *,
    handler_key: str | None = None,
) -> dict | None:
    """裁剪 settlement_config：去掉掉落概率、result_source 等内部字段。"""
    if not config or not isinstance(config, dict):
        return config
    allowed_keys = _PLAYER_SETTLEMENT_CONFIG_KEYS
    if handler_key == 'transcendent_court_preview':
        allowed_keys = allowed_keys | {'required_level'}
    data = {
        str(k): v
        for k, v in config.items()
        if str(k) in allowed_keys and v is not None
    }
    if 'input_materials' in data:
        data['input_materials'] = _material_amounts_for_client(data['input_materials'])
    return data


def _material_amounts_for_client(value: object) -> dict:
    """材料名称是动态键，数量只接受标量，不透传配置中的嵌套元数据。"""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): amount for key, amount in value.items()
        if isinstance(amount, (int, float)) and not isinstance(amount, bool)
    }


def sanitize_upgrade_config_for_client(config: dict | None) -> dict | None:
    """仅保留升级面板、槽位回退计算和治疗物品开关使用的配置。"""
    if not isinstance(config, dict):
        return None
    data = {
        key: config[key]
        for key in ('max_level', 'description', 'usage_slots_bonus')
        if key in config
    }
    if 'cost' in config:
        data['cost'] = _material_amounts_for_client(config['cost'])
    effects = config.get('handler_effects')
    if isinstance(effects, dict) and 'item_treatment' in effects:
        data['handler_effects'] = {'item_treatment': bool(effects['item_treatment'])}
    return data
