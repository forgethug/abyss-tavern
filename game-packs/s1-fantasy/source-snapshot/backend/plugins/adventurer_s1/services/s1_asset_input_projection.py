"""Omit only explicitly neutral asset metadata from AI views, never source snapshots."""
from copy import deepcopy


ASSET_INPUT_DEFAULTS_RULE = (
    '以下仅解释已有资产输入的压缩显示：未列出的 asset_dependencies 表示无依赖，recovery_state 为 none，'
    'recovering 为 false，recovery_minutes 为 0；数量、剩余次数及非默认状态按各项明确值读取。'
    'recovery_minutes 是使用后需要等待的静态时长，不是本次剩余冷却；'
    '出发时只有 recovering=true 才处于计时恢复中。recovering=false 或省略时，'
    '不因时长为正就判为正在冷却；本场新冷却由该资产实际发动及原触发条件建立。'
    '新输出对象的必填字段与空值按本次任务的输出合同完整填写，另行判断。'
)


def compact_asset_state(row):
    result = deepcopy(row)
    if not isinstance(result, dict):
        return result
    if 'asset_dependencies' in result and result['asset_dependencies'] in (None, []):
        result.pop('asset_dependencies')
    if result.get('recovery_state') == 'none':
        result.pop('recovery_state')
    if result.get('recovering') is False:
        result.pop('recovering')
    if type(result.get('recovery_minutes')) is int and result['recovery_minutes'] == 0:
        result.pop('recovery_minutes')
    return result


def pack_capability_rows(rows):
    """Input-only column table; every value and independent source survives exactly."""
    columns = ('capability_index', 'source_type', 'mechanism_description', 'runtime_state')
    if (not isinstance(rows, list) or len(rows) < 8
            or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows)):
        return deepcopy(rows)
    return {'columns': list(columns), 'rows': [[deepcopy(row[key]) for key in columns] for row in rows]}
