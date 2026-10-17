"""探险组队节流：按剩余时间动态匀速消耗日配额。

大局观由每日配额硬闸（发车时创建 ExpeditionLog）兜底；本模块调节
`seed_empty_parties_for_rank_distribution` 的开队频率，让全天发车速率尽量
平稳，避免「晚高峰把配额提前榨干后整段零进本」：

- 按北京时间日进度计算「理想累计发车数」ideal = quota × day_progress；
- 落后 / 准点（committed <= ideal）全速种队；超前则强刹车（CBR），把配额匀速
  留到后续时段，使任意时刻 committed ≈ ideal、配额接近日终才用尽；
- 发车余额按「剩余额度 / 当天剩余时间」重新摊平；每轮最多发 1 支，且不突破日配额。
"""
from runtime_core.time import utc_now as _portable_utc_now
import random
from datetime import datetime, timedelta, timezone

from utils.time_utils import BEIJING_TZ

# CBR：committed 超前 ideal 时的概率刹车地板。越小越接近匀速、越能把配额留到后段；
# 保留非零地板避免超前期间长时间连续跳过 tick。
_PACE_AHEAD_FLOOR = 0.06
def _dispatch_tick_seconds() -> int:
    """当前自动发车 tick 秒数，用于计算单轮配速放行量。"""
    try:
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        minutes = int(S1SettingsService.get('expedition_dispatch_interval_minutes') or 1)
    except Exception:
        minutes = 1
    return max(1, minutes) * 60


def get_beijing_day_start_utc() -> datetime:
    """当前北京日历日 0 点，转为 naive UTC（与库内 UTC timestamp 一致）。"""
    now_bj = datetime.now(BEIJING_TZ)
    day_start_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_bj.astimezone(timezone.utc).replace(tzinfo=None)


def compute_day_progress_beijing(now_bj: datetime | None = None) -> float:
    """北京时间当日已过比例 0~1。"""
    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    day_start = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now_bj - day_start).total_seconds()
    return min(1.0, max(0.0, elapsed / 86400.0))


def is_slow_period(now_bj: datetime | None = None) -> bool:
    """北京时间慢时段（默认 02:00–09:00）。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    if not bool(S1SettingsService.get('slow_period_enabled')):
        return False
    start_h = int(S1SettingsService.get('slow_period_start_hour'))
    end_h = int(S1SettingsService.get('slow_period_end_hour'))
    hour = now_bj.hour
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def is_fast_period(now_bj: datetime | None = None) -> bool:
    """北京时间快时段；支持跨午夜。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    if not bool(S1SettingsService.get('fast_period_enabled')):
        return False
    # SettingsService 已负责缺省回退；0 点是有效配置，不能按假值替换。
    start_h = int(S1SettingsService.get('fast_period_start_hour'))
    end_h = int(S1SettingsService.get('fast_period_end_hour'))
    if start_h <= end_h:
        return start_h <= now_bj.hour < end_h
    return now_bj.hour >= start_h or now_bj.hour < end_h


def compute_fast_period_factor(now_bj: datetime | None = None) -> float:
    """当前快时段倍率；非快时段固定为 1。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    if not is_fast_period(now_bj):
        return 1.0
    try:
        factor = float(S1SettingsService.get('fast_period_factor') or 2.0)
    except (TypeError, ValueError):
        factor = 2.0
    return max(1.0, min(10.0, factor))


def count_queued_departures() -> int:
    """今日仍会发车的 forming 队：已满员且已设 departure_time（确定性消耗配额）。"""
    from plugins.adventurer_s1.models import AdventurerS1Party

    return AdventurerS1Party.query.filter(
        AdventurerS1Party.status == 'forming',
        AdventurerS1Party.departure_time.isnot(None),
    ).count()


def get_committed_expedition_count(used: int | None = None, queued: int | None = None) -> tuple[int, int, int]:
    """返回 (used, queued, committed=used+queued)。"""
    if used is None or queued is None:
        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
            get_daily_expedition_quota_state,
        )
        used, _ = get_daily_expedition_quota_state()
        queued = count_queued_departures()
    return used, queued, used + queued


def minutes_since_last_departure() -> float:
    """距今日最近一次发车（最近一条 ExpeditionLog 创建）的分钟数；今日尚无则 +inf。"""
    from sqlalchemy import func

    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog

    today_start = get_beijing_day_start_utc()
    last = db.session.query(func.max(AdventurerS1ExpeditionLog.created_at)).filter(
        AdventurerS1ExpeditionLog.created_at >= today_start,
    ).scalar()
    if last is None:
        return float('inf')
    return (_portable_utc_now() - last).total_seconds() / 60.0


def _resolve_slow_period_weights() -> tuple[int, int, float]:
    """读取慢时段配置 (start_hour, end_hour, factor)；factor∈[0,1]。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    start_h = int(S1SettingsService.get('slow_period_start_hour'))
    end_h = int(S1SettingsService.get('slow_period_end_hour'))
    try:
        factor = float(S1SettingsService.get('slow_period_factor'))
    except (TypeError, ValueError):
        factor = 0.25
    if not bool(S1SettingsService.get('slow_period_enabled')):
        factor = 1.0
    return start_h, end_h, max(0.0, min(1.0, factor))


def _hour_weight(hour: int, start_h: int, end_h: int, factor: float) -> float:
    """该小时的发车权重：慢时段（凌晨）= factor，其余 = 1.0。"""
    if start_h <= end_h:
        in_slow = start_h <= hour < end_h
    else:  # 跨午夜
        in_slow = hour >= start_h or hour < end_h
    return factor if in_slow else 1.0


def compute_dispatch_allowance(
    used: int,
    quota: int,
    gap_minutes: float,
    now_bj: datetime | None = None,
) -> int:
    """把剩余额度平均摊到今日剩余时间，到间隔时仅放行 1 支。

    没有队伍可发时，未使用额度会自然滚入后续时段；下一次有积压时，所需间隔会按
    「剩余秒数 / 剩余额度」缩短，但不会在同一个 scheduler tick 内批量追发。
    """
    if quota <= 0:
        return 1_000_000
    remaining = quota - used
    if remaining <= 0:
        return 0
    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    tomorrow_bj = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_left = max(0.0, (tomorrow_bj - now_bj).total_seconds())
    fast_factor = compute_fast_period_factor(now_bj)
    required_gap_minutes = seconds_left / remaining / 60.0 / fast_factor
    return max(1, int(fast_factor)) if gap_minutes >= required_gap_minutes else 0


def compute_quota_seed_factor(
    used: int | None = None,
    queued: int | None = None,
    now_bj: datetime | None = None,
) -> float:
    """CBR 配额因子：落后 / 准点全速，超前强刹车，使配额匀速消耗至日终。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    quota = int(S1SettingsService.get('daily_expedition_quota') or 0)
    if quota <= 0:
        return 1.0

    used, queued, committed = get_committed_expedition_count(used, queued)
    remaining = quota - committed
    if remaining <= 0:
        return 0.0  # 耗尽兜底单独在 should_run_party_seed / dispatch 处理

    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)

    progress = compute_day_progress_beijing(now_bj)
    ideal = quota * progress
    if committed <= ideal:
        return 1.0  # 落后 / 准点：全速追

    # 超前：CBR 强刹车——超出「此刻到日终的剩余预算」越多，压得越狠
    overshoot = committed - ideal
    headroom = max(1.0, quota - ideal)
    ahead_ratio = min(1.0, overshoot / headroom)
    return max(_PACE_AHEAD_FLOOR, 1.0 - ahead_ratio)


def compute_slow_period_factor(now_bj: datetime | None = None) -> float:
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    if now_bj is None:
        now_bj = datetime.now(BEIJING_TZ)
    # 两段误配重叠时快时段优先，避免倍率互相抵消造成难以理解的实际速度。
    if not is_slow_period(now_bj) or is_fast_period(now_bj):
        return 1.0

    try:
        base_slow = float(S1SettingsService.get('slow_period_factor'))
    except (TypeError, ValueError):
        base_slow = 0.25
    base_slow = max(0.0, min(1.0, base_slow))

    quota = int(S1SettingsService.get('daily_expedition_quota') or 0)
    if quota > 0:
        _, _, committed = get_committed_expedition_count()
        ideal = quota * compute_day_progress_beijing(now_bj)
        if committed <= ideal:
            return max(base_slow, 0.5)
    return base_slow


def should_run_party_seed() -> tuple[bool, dict]:
    """是否执行本轮 seed_empty_parties（概率性）；并入 forming 队始终执行。"""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    now_bj = datetime.now(BEIJING_TZ)
    quota = int(S1SettingsService.get('daily_expedition_quota') or 0)
    used, queued, committed = get_committed_expedition_count()
    remaining = max(0, quota - committed) if quota > 0 else 0

    slow_factor = compute_slow_period_factor(now_bj=now_bj)
    if quota > 0 and remaining <= 0:
        quota_factor = 0.0
        allow_prob = 0.0
    else:
        quota_factor = compute_quota_seed_factor(now_bj=now_bj)
        allow_prob = max(
            0.0,
            min(1.0, quota_factor * slow_factor * compute_fast_period_factor(now_bj)),
        )

    allow = random.random() < allow_prob
    progress = compute_day_progress_beijing(now_bj)
    ideal = round(quota * progress, 2) if quota > 0 else 0

    return allow, {
        'allow': allow,
        'allow_probability': round(allow_prob, 4),
        'quota_factor': round(quota_factor, 4),
        'slow_factor': round(slow_factor, 4),
        'slow_period': is_slow_period(now_bj),
        'fast_factor': round(compute_fast_period_factor(now_bj), 4),
        'fast_period': is_fast_period(now_bj),
        'overflow_drain': False,
        'daily_used': used,
        'queued_departures': queued,
        'committed': committed,
        'remaining_quota_slots': remaining,
        'day_progress': round(progress, 4),
        'ideal_committed': ideal,
    }


def get_dispatch_diagnosis() -> dict:
    """只读发车诊断：动态所需间隔、当前放行量、调度配置和满员积压。

    复用真实 dispatch 的 `compute_dispatch_allowance` 公式；不写库。当日 + 昨日发车日志
    是有界小集合，一次拉取后在内存按北京小时分桶。
    """
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        get_daily_expedition_quota_state,
    )
    from plugins.adventurer_s1.services.adventurer_s1_party_service import get_departed_parties
    from extensions import db

    now_bj = datetime.now(BEIJING_TZ)
    cur_h = now_bj.hour
    used, quota = get_daily_expedition_quota_state()
    start_h, end_h, factor = _resolve_slow_period_weights()
    weights = [_hour_weight(h, start_h, end_h, factor) for h in range(24)]

    today_start_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_bj.astimezone(timezone.utc).replace(tzinfo=None)
    yday_start_utc = today_start_utc - timedelta(days=1)

    rows = db.session.query(
        AdventurerS1ExpeditionLog.created_at,
        AdventurerS1ExpeditionLog.status,
    ).filter(AdventurerS1ExpeditionLog.created_at >= yday_start_utc).all()

    today_dispatched = [0] * 24
    today_failed = [0] * 24
    status_breakdown: dict[str, int] = {}
    yday_total = 0
    yday_evening = [0] * 24

    for created_at, status in rows:
        bj = created_at + timedelta(hours=8)
        if created_at >= today_start_utc:
            today_dispatched[bj.hour] += 1
            if status == 'failed':
                today_failed[bj.hour] += 1
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
        else:
            yday_total += 1
            if bj.hour >= 18:
                yday_evening[bj.hour] += 1

    hourly = []
    running = 0
    for h in range(24):
        running += today_dispatched[h]
        hourly.append({
            'hour': h,
            'dispatched': today_dispatched[h],
            'failed': today_failed[h],
            'used_cum': running,
            'is_slow': weights[h] < 1.0,
            'is_future': h > cur_h,
        })

    remaining = max(0, quota - used)
    seconds_left = (today_start_bj + timedelta(days=1) - now_bj).total_seconds()
    fast_factor = compute_fast_period_factor(now_bj)
    required_gap_minutes = round(seconds_left / remaining / 60.0 / fast_factor, 2) if remaining > 0 else None
    gap_minutes = minutes_since_last_departure()
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    dispatch_tick_minutes = int(S1SettingsService.get('expedition_dispatch_interval_minutes') or 1)
    cap_now = max(1, int(fast_factor)) if remaining > 0 else 0
    allowance_now = compute_dispatch_allowance(used, quota, gap_minutes, now_bj) if quota > 0 else None

    # 积压队细节（满员 + 已过点仍 forming，即被配速压住没发的）：最久等待 / 按 rank 档分布 / 最久 Top
    from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_dungeon_rank
    now_utc = _portable_utc_now()
    departed = get_departed_parties()
    backlog_items = []
    backlog_by_rank: dict[int, int] = {}
    backlog_max_overdue = 0.0
    for p in departed:
        overdue = (now_utc - p.departure_time).total_seconds() / 60.0 if p.departure_time else 0.0
        backlog_max_overdue = max(backlog_max_overdue, overdue)
        rank = get_s1_dungeon_rank(p.target_dungeon)
        backlog_by_rank[rank] = backlog_by_rank.get(rank, 0) + 1
        backlog_items.append({
            'party_id': p.id,
            'target_dungeon': p.target_dungeon,
            'rank': rank,
            'overdue_minutes': round(overdue, 1),
        })
    backlog_items.sort(key=lambda x: x['overdue_minutes'], reverse=True)

    return {
        'quota': quota,
        'used_today': used,
        'bj_now': now_bj.strftime('%Y-%m-%d %H:%M'),
        'slow_period': {'start_hour': start_h, 'end_hour': end_h, 'factor': factor},
        'fast_period': {
            'active': is_fast_period(now_bj),
            'factor': fast_factor,
            'start_hour': int(S1SettingsService.get('fast_period_start_hour')),
            'end_hour': int(S1SettingsService.get('fast_period_end_hour')),
        },
        'cap_now': cap_now,
        'allowance_now': allowance_now,
        'required_gap_minutes': required_gap_minutes,
        'minutes_since_last_departure': None if gap_minutes == float('inf') else round(gap_minutes, 2),
        'dispatch_tick_minutes': dispatch_tick_minutes,
        'backlog_due': len(departed),
        'backlog_max_overdue_minutes': round(backlog_max_overdue, 1),
        'backlog_by_rank': {str(k): v for k, v in sorted(backlog_by_rank.items())},
        'backlog_items': backlog_items[:15],
        'status_breakdown': status_breakdown,
        'yesterday_total': yday_total,
        'yesterday_evening': [{'hour': h, 'count': yday_evening[h]} for h in range(18, 24)],
        'hourly': hourly,
    }
