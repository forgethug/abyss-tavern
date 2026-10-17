"""An operator's conditional process, evaluated at the persisted departure time."""
from datetime import datetime, timezone

from flask import current_app
from utils.time_utils import to_utc_iso


def _limit(name):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_CAMPAIGN_TIME_' + name
    return int(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))


def normalize_action_window(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('action_window 必须为对象或 null')
    anchor = value.get('anchor')
    if not isinstance(anchor, str) or anchor not in ('campaign_started', 'campaign_discovered'):
        raise ValueError('行动窗口必须从战役创建或首次发现计时')
    hours = value.get('after_hours')
    if type(hours) is not int or not 1 <= hours <= _limit('MAX_HOURS'):
        raise ValueError('行动窗口小时数超出配置范围')
    result = {'anchor': anchor, 'after_hours': hours}
    for key in ('condition', 'development'):
        text = value.get(key)
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > _limit('MAX_TEXT'):
            raise ValueError('行动窗口的条件与过程必须为有界非空文本')
        result[key] = text.strip()
    return result


def _timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError('行动窗口缺少准确 UTC 时间')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError as exc:
        raise ValueError('行动窗口时间格式无效') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('行动窗口时间必须包含时区')
    return parsed.astimezone(timezone.utc)


def _utc_iso(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc_iso(value)


def project_action_window_context(value):
    if not isinstance(value, dict):
        raise ValueError('冻结行动窗口必须为对象')
    window = normalize_action_window(value.get('window'))
    if window is None:
        raise ValueError('冻结行动窗口缺少配置')
    observed = _timestamp(value.get('observed_at'))
    anchor = _timestamp(value.get('anchor_at'))
    if observed < anchor:
        raise ValueError('行动窗口出发时间早于计时起点')
    elapsed = int((observed - anchor).total_seconds())
    return {
        'window': window, 'anchor_at': _utc_iso(anchor), 'observed_at': _utc_iso(observed),
        'elapsed_seconds': elapsed, 'threshold_reached': elapsed >= window['after_hours'] * 3600,
    }


def freeze_action_window(context, node, dispatch, log):
    # Discovery still receives only its local revelation; operational windows are private.
    if context['discovery'] is True or node.get('action_window') is None:
        return
    window = normalize_action_window(node['action_window'])
    anchor = dispatch.created_at if window['anchor'] == 'campaign_started' else dispatch.discovered_at
    context['action_window_context'] = project_action_window_context({
        'window': window, 'anchor_at': _utc_iso(anchor),
        'observed_at': _utc_iso(log.created_at),
    })


ACTION_WINDOW_RULE = '''
冻结 action_window_context 仅描述本阶段、本地图的有条件世界过程。anchor_at 是本区战役实际计时起点，observed_at 是本场已持久化的出发时间；elapsed_seconds 与 threshold_reached 只证明经过了多少时间，不证明条件满足、过程发生或任务失败。重试沿用这份时间快照。
window.condition 说明过程成立所需的世界内前提，development 说明这些前提持续有效且准备时间足够时可能推进的过程。依据精确当地现状、已有能力、已发布结果及本场因果判断前提是否曾中断、过程是否已完成或被阻止；时间起点不证明所有前提从那一刻起一直有效。前提或持续时间不明时保持未知，通过现场行动核实。已经成立的成果、损耗与人物终态继续有效，同一已完成过程不因再次经过阈值重复发生。
正文建立的新后果沿原世界事实与地点/人物合同保存；节点成败仍按 stage_task 的条件及正文证据判断。窗口配置不授予新能力，不提高既定力量，不指定胜负，也不自动成为角色或酒馆知识。公开报告只包含实际可传播的发现；时间信息本身不授权泄露秘密或未来阶段。
'''.strip()


ACTION_WINDOW_OBSERVER_RULE = '''
同一冻结输入含 action_window_context 时，依据相同时间与当地事实核对实际采用的过程前提、已有成果和公开知识。若仅因阈值到达就无据改写世界终态或节点成败，按现有正文或 world_fact_updates 修复出口处理；不为制造两区差异改写合法结果，不要求尚未成立的过程一定发生。
'''.strip()
