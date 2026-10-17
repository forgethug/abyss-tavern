"""S1 trait recovery driven by structured metadata, not description text."""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta
from time import monotonic
from typing import Iterable, List, Optional

from flask import current_app

from utils.time_utils import to_utc_iso


NATURAL_RECOVERY_POLICIES = {'rest', 'sleep'}
NON_NATURAL_RECOVERY_POLICIES = {'medical', 'purify', 'special'}
VALID_RECOVERY_POLICIES = {'none', 'rest', 'sleep', *NON_NATURAL_RECOVERY_POLICIES}
# 宿舍自然休息无法自行消退，需医疗/净化/特殊机缘等流程处理的顽固负面特质。
STUBBORN_TRAIT_POLICIES = frozenset({'none', *NON_NATURAL_RECOVERY_POLICIES})
DORM_RECOVERY_STAGES = frozenset({'light', 'medium'})
VALID_RECOVERY_STAGES = {'light', 'medium', 'heavy', 'trace'}
# 探险 AI / 申诉 AI 偶发 critical，语义等同 heavy（医疗级顽固负面）
_RECOVERY_STAGE_ALIASES = {'critical': 'heavy'}
TRACE_RECOVERY_HOURS_MIN = 1
TRACE_RECOVERY_HOURS_MAX = 4


def list_stubborn_negative_traits(member) -> list:
    """列出成员当前无法靠宿舍自然休息自行消退的活跃负面特质（未归档）。"""
    result = []
    for trait in getattr(member, 'traits', []) or []:
        if getattr(trait, 'is_archived', False):
            continue
        policy = (getattr(trait, 'recovery_policy', None) or 'none').strip().lower()
        if policy in STUBBORN_TRAIT_POLICIES:
            result.append(trait)
    return result


def list_trait_relief_candidates(member) -> list:
    """祛厄药庐：后端不判断正负面，只把未归档特质交给 AI 自选。"""
    result = []
    for trait in getattr(member, 'traits', []) or []:
        if getattr(trait, 'is_archived', False):
            continue
        result.append(trait)
    return result


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _configured_cycle_hours() -> int:
    return max(
        1,
        _coerce_int(
            current_app.config.get('ADVENTURER_S1_TRAIT_RECOVERY_CYCLE_HOURS', 6),
            6,
        ),
    )


def _sanatorium_speed_multiplier() -> int:
    configured = _coerce_int(
        current_app.config.get('ADVENTURER_S1_SANATORIUM_RECOVERY_SPEED_MULTIPLIER', 0),
        0,
    )
    if configured > 0:
        return configured
    dorm_hours = _configured_cycle_hours()
    sanatorium_hours = max(
        1,
        _coerce_int(
            current_app.config.get('ADVENTURER_S1_SANATORIUM_CYCLE_HOURS', 3),
            3,
        ),
    )
    return max(1, dorm_hours // sanatorium_hours)


def is_sanatorium_resting(member) -> bool:
    """True when member is occupying a built sanatorium bed (building usage)."""
    if getattr(member, 'status', None) != 'building':
        return False
    inst = getattr(member, 'building_instance', None)
    if inst is None or getattr(inst, 'status', None) != 'built':
        return False
    tpl = getattr(inst, 'template', None)
    if tpl is None or getattr(tpl, 'handler_key', None) != 'sanatorium':
        return False
    return getattr(member, 'building_assigned_at', None) is not None


def is_dorm_bed_resting(member, *, tavern_seated: bool = False) -> bool:
    """True when natural trait recovery cycles should accumulate (detail + settlement)."""
    if tavern_seated:
        return False
    if getattr(member, 'status', None) != 'resting':
        return False
    if getattr(member, 'dorm_room_id', None) is None:
        return False
    if getattr(member, 'bed_index', None) is None:
        return False
    if hasattr(member, '_should_suspend_dorm_bed_recovery') and member._should_suspend_dorm_bed_recovery(
        tavern_seated=tavern_seated,
    ):
        return False
    return True


def _sanatorium_elapsed_seconds(member, *, now: datetime) -> int:
    if not is_sanatorium_resting(member):
        return 0
    start = getattr(member, 'building_assigned_at', None)
    if not start:
        return 0
    return max(0, int((now - start).total_seconds()))


def flush_sanatorium_rest_session(
    member,
    *,
    now: datetime | None = None,
) -> None:
    """Merge the current sanatorium session into trait_recovery_rest_seconds (2× effective)."""
    now = now or _portable_utc_now()
    elapsed_seconds = _sanatorium_elapsed_seconds(member, now=now)
    if elapsed_seconds <= 0:
        return
    multiplier = _sanatorium_speed_multiplier()
    _set_banked_rest_seconds(
        member,
        _get_banked_rest_seconds(member) + elapsed_seconds * multiplier,
    )
    member.building_assigned_at = now


def _flush_sanatorium_stamina_hp(member) -> None:
    """将疗养所中惰性恢复的精力/血量落账（在 building_assigned_at 被重置之前调用）。"""
    from plugins.adventurer_s1.services.adventurer_s1_recovery_service import flush_recovery_to_db
    flush_recovery_to_db(member)


def _rest_elapsed_seconds(member, *, now: datetime) -> Optional[float]:
    start = getattr(member, 'rest_start_time', None) or getattr(member, 'stamina_updated_at', None)
    if not start:
        return None
    return max(0.0, (now - start).total_seconds())


def _get_banked_rest_seconds(member) -> int:
    return max(0, _coerce_int(getattr(member, 'trait_recovery_rest_seconds', 0), 0))


def _set_banked_rest_seconds(member, seconds: int) -> None:
    member.trait_recovery_rest_seconds = max(0, int(seconds))


def trait_has_natural_dorm_recovery(obj) -> bool:
    """True when trait uses dorm-bed cycle recovery (light/medium)."""
    if isinstance(obj, dict):
        policy = str(obj.get('recovery_policy') or 'none').strip().lower()
        stage = str(obj.get('recovery_stage') or '').strip().lower()
    else:
        policy = str(getattr(obj, 'recovery_policy', None) or 'none').strip().lower()
        stage = str(getattr(obj, 'recovery_stage', None) or '').strip().lower()
    return policy in NATURAL_RECOVERY_POLICIES and stage in DORM_RECOVERY_STAGES


def _trait_recovery_progress(trait) -> int:
    return max(0, _coerce_int(getattr(trait, 'recovery_progress', 0), 0))


def _effective_recovery_rest_baseline(trait) -> int | None:
    """Baseline only gates the first cycle after recovery tags are enabled."""
    if getattr(trait, 'recovery_rest_baseline_persistent', False):
        return _coerce_int(getattr(trait, 'recovery_rest_baseline_seconds', 0), 0)
    if _trait_recovery_progress(trait) > 0:
        return None
    baseline = getattr(trait, 'recovery_rest_baseline_seconds', None)
    if baseline is None:
        return None
    return max(0, _coerce_int(baseline, 0))


def _repair_recovery_rest_baseline(
    trait,
    member,
    *,
    now: datetime | None = None,
    tavern_seated: bool = False,
) -> None:
    """Fix baseline rows that block recovery after cycle 1 or bank reset."""
    if getattr(trait, 'recovery_rest_baseline_persistent', False):
        return
    if _trait_recovery_progress(trait) > 0:
        if getattr(trait, 'recovery_rest_baseline_seconds', None) is not None:
            trait.recovery_rest_baseline_seconds = None
        return
    baseline = getattr(trait, 'recovery_rest_baseline_seconds', None)
    if baseline is None:
        return
    now = now or _portable_utc_now()
    total = get_trait_recovery_total_rest_seconds(
        member, now=now, tavern_seated=tavern_seated,
    )
    if _coerce_int(baseline, 0) > total:
        trait.recovery_rest_baseline_seconds = None


def get_trait_eligible_rest_seconds(
    member,
    trait,
    *,
    now: datetime,
    tavern_seated: bool = False,
) -> int:
    """Rest seconds that count toward this trait's dorm recovery cycles."""
    _repair_recovery_rest_baseline(
        trait, member, now=now, tavern_seated=tavern_seated,
    )
    total = get_trait_recovery_total_rest_seconds(
        member, now=now, tavern_seated=tavern_seated,
    )
    baseline = _effective_recovery_rest_baseline(trait)
    if baseline is None:
        return total
    return max(0, total - baseline)


def maybe_set_recovery_rest_baseline(
    trait,
    member,
    *,
    had_natural_dorm_recovery: bool,
    now: datetime | None = None,
) -> None:
    """When dorm recovery is newly enabled, ignore rest banked before activation."""
    if had_natural_dorm_recovery or not trait_has_natural_dorm_recovery(trait):
        return
    if getattr(trait, 'recovery_rest_baseline_seconds', None) is not None:
        return
    now = now or _portable_utc_now()
    trait.recovery_rest_baseline_seconds = max(
        0,
        get_trait_recovery_total_rest_seconds(member, now=now),
    )


def get_trait_recovery_total_rest_seconds(
    member,
    *,
    now: datetime,
    tavern_seated: bool = False,
) -> int:
    """Total effective rest seconds toward the next recovery cycle(s).

    Includes persisted bank plus active dorm-bed session (1×) and sanatorium
    session (configurable multiplier, default 2×).
    """
    total = _get_banked_rest_seconds(member)
    if is_dorm_bed_resting(member, tavern_seated=tavern_seated):
        elapsed_seconds = _rest_elapsed_seconds(member, now=now)
        if elapsed_seconds is not None:
            total += int(elapsed_seconds)
    if is_sanatorium_resting(member):
        total += _sanatorium_elapsed_seconds(member, now=now) * _sanatorium_speed_multiplier()
    return total


def flush_dorm_rest_session(
    member,
    *,
    now: datetime | None = None,
    tavern_seated: bool = False,
) -> None:
    """Merge the current dorm-bed session into ``trait_recovery_rest_seconds``."""
    now = now or _portable_utc_now()
    if not is_dorm_bed_resting(member, tavern_seated=tavern_seated):
        return
    elapsed_seconds = _rest_elapsed_seconds(member, now=now)
    if elapsed_seconds is None or elapsed_seconds <= 0:
        return
    _set_banked_rest_seconds(
        member,
        _get_banked_rest_seconds(member) + int(elapsed_seconds),
    )
    member.rest_start_time = now


def reset_trait_recovery_rest_tracking(member) -> None:
    """Clear cumulative + current-session rest tracking (called on expedition departure)."""
    _set_banked_rest_seconds(member, 0)
    member.rest_start_time = None
    for trait in getattr(member, 'traits', None) or []:
        if getattr(trait, 'is_archived', False):
            continue
        if not trait_has_natural_dorm_recovery(trait):
            continue
        if getattr(trait, 'recovery_rest_baseline_persistent', False):
            trait.recovery_rest_baseline_persistent = False
            trait.recovery_rest_baseline_seconds = None
        if _trait_recovery_progress(trait) <= 0:
            trait.recovery_rest_baseline_seconds = None


def get_dorm_rest_recovery_points(
    member,
    *,
    now: datetime | None = None,
    tavern_seated: bool = False,
) -> int:
    """Return completed natural recovery cycles from cumulative dorm bed rest.

    Rest time accrues across multiple resting segments while assigned to a dorm
    bed (excluding tavern seating). Description text is never inspected.
    """
    now = now or _portable_utc_now()
    cycle_seconds = _configured_cycle_hours() * 3600
    total_seconds = get_trait_recovery_total_rest_seconds(
        member, now=now, tavern_seated=tavern_seated,
    )
    return int(total_seconds // cycle_seconds)


def _format_duration_seconds(seconds: float) -> str:
    total_sec = max(0, int(seconds + 0.999))  # ceil, match frontend
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f'{hours}小时{minutes}分'
    if minutes > 0:
        return f'{minutes}分{secs}秒'
    return f'{secs}秒'


def _coerce_trace_hours(value) -> int:
    hours = _coerce_int(value, 0)
    if hours < TRACE_RECOVERY_HOURS_MIN:
        return TRACE_RECOVERY_HOURS_MIN
    if hours > TRACE_RECOVERY_HOURS_MAX:
        return TRACE_RECOVERY_HOURS_MAX
    return hours


def _parse_expires_at(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().rstrip('Z')
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def build_trait_recovery_countdown(
    trait_entry: dict,
    member,
    *,
    tavern_seated: bool = False,
    now: datetime | None = None,
    recovery_rest_baseline_seconds: int | None = None,
    recovery_rest_baseline_persistent: bool = False,
) -> Optional[dict]:
    """Build detail-API recovery snapshot for one visible trait entry."""
    policy = str(trait_entry.get('recovery_policy') or 'none').strip().lower()
    stage = str(trait_entry.get('recovery_stage') or '').strip().lower()

    if policy in NATURAL_RECOVERY_POLICIES and stage == 'trace':
        now = now or _portable_utc_now()
        expires_at = _parse_expires_at(trait_entry.get('recovery_expires_at'))
        hours = _coerce_trace_hours(trait_entry.get('recovery_hours') or 2)
        if expires_at is None:
            expires_at = now + timedelta(hours=hours)
        seconds_left = max(0, int((expires_at - now).total_seconds()))
        policy_label = '睡眠' if policy == 'sleep' else '静养'
        return {
            'progress': 0,
            'required': 0,
            'remaining_cycles': 0,
            'accumulating': False,
            'seconds_to_next_cycle': seconds_left,
            'completed_cycles_this_rest': 0,
            'cycle_hours': hours,
            'recovery_kind': 'trace',
            'status_text': (
                f'⏳ 痕迹级负伤 · {policy_label} · '
                f'{_format_duration_seconds(seconds_left)}后自行消退'
            ),
            'hint_text': '不占用宿舍床位，可正常探险；若进本后伤势恶化，将不再按痕迹级处理',
        }

    if policy not in NATURAL_RECOVERY_POLICIES or stage not in {'light', 'medium'}:
        return None

    raw_required = _coerce_int(trait_entry.get('recovery_required'), 0)
    if stage == 'light':
        required = 1
    elif stage == 'medium':
        required = raw_required if raw_required in (2, 3) else 2
    else:
        return None

    progress = max(0, _coerce_int(trait_entry.get('recovery_progress'), 0))
    remaining_cycles = max(0, required - progress)
    if remaining_cycles <= 0:
        return None

    now = now or _portable_utc_now()
    cycle_hours = _configured_cycle_hours()
    cycle_seconds = cycle_hours * 3600
    sanatorium_accumulating = is_sanatorium_resting(member)
    dorm_accumulating = is_dorm_bed_resting(member, tavern_seated=tavern_seated)
    accumulating = dorm_accumulating or sanatorium_accumulating
    policy_label = '睡眠' if policy == 'sleep' else '静养'
    sanatorium_hours = max(
        1,
        _coerce_int(current_app.config.get('ADVENTURER_S1_SANATORIUM_CYCLE_HOURS', 3), 3),
    )
    speed_multiplier = _sanatorium_speed_multiplier()

    total_rest_seconds = get_trait_recovery_total_rest_seconds(
        member, now=now, tavern_seated=tavern_seated,
    )
    if not recovery_rest_baseline_persistent and max(0, _coerce_int(trait_entry.get('recovery_progress'), 0)) > 0:
        baseline = None
    else:
        baseline = (
            recovery_rest_baseline_seconds
            if recovery_rest_baseline_seconds is not None
            else trait_entry.get('recovery_rest_baseline_seconds')
        )
    if baseline is not None:
        total_rest_seconds = max(0, total_rest_seconds - _coerce_int(baseline, 0))
    completed_cycles = int(total_rest_seconds // cycle_seconds)
    remainder = total_rest_seconds % cycle_seconds
    seconds_to_next: Optional[int] = None
    if total_rest_seconds > 0 or accumulating:
        if total_rest_seconds > 0 and remainder == 0:
            # 已累计整周期，读路径应结算；0 表示「待结算」而非「新周期刚开始」。
            seconds_to_next = 0
        elif remainder > 0:
            seconds_to_next = int(cycle_seconds - remainder)
        else:
            # 床位/疗养所中但计时刚清零（例如上一周期结算后）——距下一周期仍是完整 cycle。
            seconds_to_next = cycle_seconds

    progress_text = f'恢复进度 {progress}/{required}'

    if sanatorium_accumulating and seconds_to_next is not None:
        real_seconds_to_next = max(0, int(seconds_to_next / max(1, speed_multiplier)))
        status_text = (
            f'🌿 疗养{policy_label}中 · {progress_text} · '
            f'下周期 {_format_duration_seconds(real_seconds_to_next)}'
        )
        if completed_cycles > 0:
            hint_text = (
                f'已累计 {completed_cycles} 个完整周期（疗养所每 {sanatorium_hours} 小时 1 周期，'
                f'宿舍需 {cycle_hours} 小时）'
            )
        else:
            hint_text = f'疗养所加速：每 {sanatorium_hours} 小时实计 1 个恢复周期（宿舍需 {cycle_hours} 小时）'
    elif dorm_accumulating and seconds_to_next is not None:
        status_text = (
            f'🛏️ 宿舍{policy_label}中 · {progress_text} · '
            f'下周期 {_format_duration_seconds(seconds_to_next)}'
        )
        if completed_cycles > 0:
            hint_text = f'已累计 {completed_cycles} 个完整周期（每周期 {cycle_hours} 小时）'
        else:
            hint_text = f'每累计 {cycle_hours} 小时宿舍床位休息算 1 个周期'
    elif dorm_accumulating:
        status_text = f'🛏️ 宿舍{policy_label}中 · {progress_text} · 计时同步中'
        hint_text = '休息起点尚未就绪，请稍后刷新；若持续如此请联系管理员'
    elif tavern_seated and getattr(member, 'status', None) == 'resting':
        if seconds_to_next is not None and total_rest_seconds > 0:
            status_text = (
                f'🍺 酒馆中 · {progress_text} · '
                f'下周期 {_format_duration_seconds(seconds_to_next)}'
            )
            hint_text = '占用酒吧座位时不继续累计，已有进度保留；回床位后继续'
        else:
            status_text = f'🍺 酒馆中 · {progress_text} · 回床位后继续累计'
            hint_text = '占用酒吧座位时不累计周期，回到宿舍床位后继续累计'
    elif seconds_to_next is not None and total_rest_seconds > 0:
        status_text = (
            f'💤 可{policy_label}恢复 · {progress_text} · '
            f'下周期 {_format_duration_seconds(seconds_to_next)}'
        )
        hint_text = f'已有累计进度；回到宿舍床位或入住疗养所可继续累计'
    else:
        status_text = f'💤 可{policy_label}恢复 · {progress_text} · 需在宿舍床位或疗养所休息才能累计'
        hint_text = (
            f'宿舍每 {cycle_hours} 小时 1 周期；疗养所加速，每 {sanatorium_hours} 小时 1 周期'
        )

    return {
        'progress': progress,
        'required': required,
        'remaining_cycles': remaining_cycles,
        'accumulating': accumulating,
        'seconds_to_next_cycle': seconds_to_next,
        'completed_cycles_this_rest': completed_cycles,
        'cycle_hours': cycle_hours,
        'status_text': status_text,
        'hint_text': hint_text,
    }


def enrich_detail_trait_recovery(
    data: dict,
    member,
    *,
    tavern_seated: bool = False,
    now: datetime | None = None,
) -> None:
    """Attach per-trait ``recovery`` snapshots to an adventurer detail payload."""
    now = now or _portable_utc_now()
    data['trait_recovery_computed_at'] = to_utc_iso(now)
    trait_by_name = {
        t.name: t
        for t in getattr(member, 'traits', None) or []
        if not getattr(t, 'is_archived', False) and getattr(t, 'name', None)
    }
    for entry in data.get('traits') or []:
        trait_obj = trait_by_name.get(entry.get('name'))
        if trait_obj is not None:
            _repair_recovery_rest_baseline(
                trait_obj, member, tavern_seated=tavern_seated, now=now,
            )
        baseline = (
            getattr(trait_obj, 'recovery_rest_baseline_seconds', None)
            if trait_obj is not None else None
        )
        countdown = build_trait_recovery_countdown(
            entry,
            member,
            tavern_seated=tavern_seated,
            now=now,
            recovery_rest_baseline_seconds=baseline,
            recovery_rest_baseline_persistent=bool(getattr(trait_obj, 'recovery_rest_baseline_persistent', False)),
        )
        if countdown:
            entry['recovery'] = countdown


def sanitize_trait_recovery_metadata(value) -> dict:
    """Normalize AI-provided recovery metadata into a strict whitelist.

    Missing or invalid metadata becomes ``policy=none`` so legacy traits and
    malformed AI output never recover automatically by accident.
    """
    if not isinstance(value, dict):
        return {
            'recovery_policy': 'none',
            'recovery_stage': None,
            'recovery_progress': 0,
            'recovery_required': None,
            'recovered_desc': None,
        }

    policy = str(value.get('policy') or value.get('recovery_policy') or 'none').strip().lower()
    if policy not in VALID_RECOVERY_POLICIES:
        policy = 'none'

    stage = str(value.get('stage') or value.get('recovery_stage') or '').strip().lower()
    stage = _RECOVERY_STAGE_ALIASES.get(stage, stage)
    if stage not in VALID_RECOVERY_STAGES:
        stage = None

    recovered_desc = str(
        value.get('recovered_desc') or value.get('d') or ''
    ).strip() or None

    required = _coerce_int(value.get('required') or value.get('recovery_required'), 0)
    hours_raw = value.get('recovery_hours') if value.get('recovery_hours') is not None else value.get('hours')
    if policy not in NATURAL_RECOVERY_POLICIES or stage is None:
        policy = 'none'
        required = 0
        recovered_desc = None
    elif stage == 'trace':
        recovered_desc = None
        required = None
    elif stage == 'light':
        required = 1
    elif stage == 'medium':
        required = required if required in (2, 3) else 2
    else:
        # Heavy effects can be tagged for UI/admin context, but natural rest
        # must not remove them. Medical/purify/special flows handle those.
        policy = 'none'
        required = 0
        recovered_desc = None

    result = {
        'recovery_policy': policy,
        'recovery_stage': stage if policy != 'none' else None,
        'recovery_progress': 0,
        'recovery_required': required if policy != 'none' and stage != 'trace' else None,
        'recovered_desc': recovered_desc,
    }
    if policy != 'none' and stage == 'trace':
        result['recovery_hours'] = _coerce_trace_hours(hours_raw if hours_raw is not None else 2)
    return result


def sanitize_trait_recovery_metadata_for_appeal(value) -> dict:
    """数据申诉专用：保留 medical/purify/special 标签，不走自然休息周期逻辑。"""
    if not isinstance(value, dict):
        return {
            'recovery_policy': 'none',
            'recovery_stage': None,
            'recovery_progress': 0,
            'recovery_required': None,
            'recovered_desc': None,
        }

    policy = str(value.get('policy') or value.get('recovery_policy') or 'none').strip().lower()
    if policy not in VALID_RECOVERY_POLICIES:
        policy = 'none'

    if policy in NON_NATURAL_RECOVERY_POLICIES:
        stage = str(value.get('stage') or value.get('recovery_stage') or '').strip().lower()
        stage = _RECOVERY_STAGE_ALIASES.get(stage, stage)
        if stage not in VALID_RECOVERY_STAGES:
            stage = 'heavy'
        recovered_desc = str(value.get('recovered_desc') or '').strip() or None
        return {
            'recovery_policy': policy,
            'recovery_stage': stage,
            'recovery_progress': 0,
            'recovery_required': None,
            'recovered_desc': recovered_desc,
        }

    if policy == 'none':
        return {
            'recovery_policy': 'none',
            'recovery_stage': None,
            'recovery_progress': 0,
            'recovery_required': None,
            'recovered_desc': None,
        }

    return sanitize_trait_recovery_metadata(value)


def apply_recovery_metadata_to_trait(trait, metadata: dict | None, *, member=None) -> None:
    """Write normalized recovery metadata onto an AdventurerS1Trait instance."""
    had_dorm_recovery = trait_has_natural_dorm_recovery(trait)
    normalized = sanitize_trait_recovery_metadata(metadata)
    trait.recovery_policy = normalized['recovery_policy']
    trait.recovery_stage = normalized['recovery_stage']
    trait.recovery_progress = normalized['recovery_progress']
    trait.recovery_required = normalized['recovery_required']
    trait.recovered_desc = normalized['recovered_desc']
    if normalized.get('recovery_stage') == 'trace' and normalized['recovery_policy'] != 'none':
        hours = _coerce_trace_hours(normalized.get('recovery_hours'))
        trait.recovery_hours = hours
        trait.recovery_expires_at = _portable_utc_now() + timedelta(hours=hours)
    else:
        trait.recovery_hours = None
        trait.recovery_expires_at = None
    if member is not None:
        maybe_set_recovery_rest_baseline(
            trait,
            member,
            had_natural_dorm_recovery=had_dorm_recovery,
        )


def _archive_trait(trait, *, reason: str) -> None:
    trait.is_archived = True
    trait.archived_reason = reason
    trait.archived_at = _portable_utc_now()
    trait.recovery_policy = 'none'
    trait.recovery_stage = None
    trait.recovery_progress = 0
    trait.recovery_required = None
    trait.recovery_hours = None
    trait.recovery_expires_at = None
    trait.recovery_rest_baseline_seconds = None
    trait.recovered_desc = None


def _build_recovery_event(member_name: str, trait_name: str, action: str) -> str:
    if action == 'remove':
        return f'{member_name}经过休息后，「{trait_name}」已不再成立。'
    return f'{member_name}经过休息后，「{trait_name}」有所好转。'


def build_trait_recovery_activity_description(member_name: str, trait_name: str) -> str:
    return _build_recovery_event(member_name, trait_name, 'remove')


def _trait_required_cycles(trait) -> int:
    stage = (getattr(trait, 'recovery_stage', None) or '').strip().lower()
    if stage == 'light':
        return 1
    if stage == 'medium':
        required = _coerce_int(getattr(trait, 'recovery_required', None), 0)
        return required if required in (2, 3) else 2
    return 0


def trait_natural_remaining_cycles(trait) -> Optional[int]:
    """已在自然休息恢复期的特质，返回剩余周期数；不在恢复期返回 None。

    - light/medium：``required - progress``（最小 0）。
    - trace：视为 0（痕迹级极轻，治疗后下次休息即可消退，不应被重新拉长疗程）。
    - 其它（none/heavy/医疗/净化等非自然休息策略）：返回 None，治疗不受单调约束。

    供治疗所在治疗前快照剩余疗程，保证治疗后剩余周期单调不增。
    """
    policy = (getattr(trait, 'recovery_policy', None) or 'none').strip().lower()
    stage = (getattr(trait, 'recovery_stage', None) or '').strip().lower()
    if policy not in NATURAL_RECOVERY_POLICIES:
        return None
    if stage == 'trace':
        return 0
    if stage not in {'light', 'medium'}:
        return None
    required = _trait_required_cycles(trait)
    if required <= 0:
        return None
    progress = max(0, _coerce_int(getattr(trait, 'recovery_progress', 0), 0))
    return max(0, required - progress)


def carry_over_recovery_progress(trait, remaining_before: int) -> None:
    """治疗后保证剩余恢复周期不超过治疗前（单调不增）。

    通过继承 ``recovery_progress`` 实现：
    ``new_remaining = min(required, remaining_before)``，
    ``progress = required - new_remaining``。
    light(剩1) 因此不会被 medium(剩2) 重置拉长；剩 1 周期的伤再治疗也不会回到剩 2 周期。
    仅作用于自然休息恢复期(light/medium)；trace/非自然策略不在此处理。
    """
    policy = (getattr(trait, 'recovery_policy', None) or 'none').strip().lower()
    stage = (getattr(trait, 'recovery_stage', None) or '').strip().lower()
    if policy not in NATURAL_RECOVERY_POLICIES or stage not in {'light', 'medium'}:
        return
    required = _trait_required_cycles(trait)
    if required <= 0:
        return
    new_remaining = min(required, max(0, _coerce_int(remaining_before, 0)))
    trait.recovery_progress = max(0, required - new_remaining)


def _reset_trait_rest_baseline_after_cycle_consumed(trait, consumed_seconds=0) -> None:
    """Consumed dorm rest is reflected in recovery_progress; do not keep skipping it."""
    if getattr(trait, 'recovery_rest_baseline_persistent', False):
        trait.recovery_rest_baseline_seconds = int(trait.recovery_rest_baseline_seconds or 0) + consumed_seconds
    else:
        trait.recovery_rest_baseline_seconds = None


def _finalize_member_rest_after_cycle_settlement(
    member,
    *,
    remaining_eligible_seconds: int,
    now: datetime,
    tavern_seated: bool = False,
) -> None:
    """Persist partial progress toward the next cycle after settling due cycles."""
    old_total = get_trait_recovery_total_rest_seconds(member, now=now, tavern_seated=tavern_seated)
    for trait in getattr(member, 'traits', []) or []:
        if getattr(trait, 'recovery_rest_baseline_persistent', False):
            trait.recovery_rest_baseline_seconds = int(trait.recovery_rest_baseline_seconds or 0) + int(remaining_eligible_seconds) - old_total
    _set_banked_rest_seconds(member, max(0, int(remaining_eligible_seconds)))
    if is_sanatorium_resting(member):
        # 重置 building_assigned_at 之前，先把惰性恢复的精力/血量落账，
        # 否则前端继续用旧 building_assigned_at 计算会越算越大。
        _flush_sanatorium_stamina_hp(member)
        member.building_assigned_at = now
    elif is_dorm_bed_resting(member, tavern_seated=tavern_seated):
        member.rest_start_time = now
    else:
        member.rest_start_time = None


def _apply_recovery_to_trait(trait, *, recovery_points: int) -> str | None:
    policy = (getattr(trait, 'recovery_policy', None) or 'none').strip().lower()
    stage = (getattr(trait, 'recovery_stage', None) or '').strip().lower()
    if policy not in NATURAL_RECOVERY_POLICIES or stage not in {'light', 'medium'}:
        return None

    required = _trait_required_cycles(trait)
    if required <= 0:
        return None

    progress_before = _coerce_int(getattr(trait, 'recovery_progress', 0), 0)
    progress_after = min(required, progress_before + max(1, recovery_points))
    trait.recovery_progress = progress_after

    if stage == 'light' or progress_after >= required:
        _archive_trait(trait, reason='自然休息恢复')
        return 'remove'

    # Medium traits get one visible improvement before final removal. This is
    # still metadata-driven: recovered_desc was produced when the trait was
    # created, not inferred here from arbitrary prose.
    recovered_desc = (getattr(trait, 'recovered_desc', None) or '').strip()
    if recovered_desc:
        from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
            sync_trait_description_fields,
        )

        sync_trait_description_fields(trait, recovered_desc)
        trait.recovered_desc = None
        return 'shorten'

    # Progress itself is a successful settlement even when legacy/malformed
    # metadata omitted the optional visible intermediate description. Returning
    # an action ensures callers consume the completed rest cycle instead of
    # applying that same cycle again on every subsequent settlement.
    if progress_after > progress_before:
        return 'progress'

    return None


def apply_pre_expedition_trait_recovery(
    members: Iterable,
    *,
    recovery_points: int | None = None,
    now: datetime | None = None,
) -> List[str]:
    """Apply natural rest/sleep recovery before departure.

    Returns player-visible event strings that should be prepended to the
    expedition log.
    """
    events: List[str] = []
    member_list = list(members)
    seated_ids: set[int] = set()
    if member_list and recovery_points is None:
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
            AdventurerS1TavernSeatService,
        )
        seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(
            [getattr(m, 'id', None) for m in member_list if getattr(m, 'id', None)],
        )

    now = now or _portable_utc_now()
    live_settlement = recovery_points is None

    for member in member_list:
        tavern_seated = getattr(member, 'id', None) in seated_ids
        if live_settlement:
            flush_dorm_rest_session(member, now=now, tavern_seated=tavern_seated)
            flush_sanatorium_rest_session(member, now=now)

        if recovery_points is not None:
            points = max(0, _coerce_int(recovery_points, 0))
            if points > 0:
                member_name = getattr(member, 'name', None) or f'成员{getattr(member, "id", "?")}'
                for trait in getattr(member, 'traits', None) or []:
                    if getattr(trait, 'is_archived', False):
                        continue
                    action = _apply_recovery_to_trait(trait, recovery_points=points)
                    if action:
                        # Explicit recovery points do not consume the member's
                        # live rest bank or its transferred-trait offset.
                        _reset_trait_rest_baseline_after_cycle_consumed(trait)
                        events.append(_build_recovery_event(member_name, trait.name, action))
        else:
            cycle_seconds = _configured_cycle_hours() * 3600
            member_name = getattr(member, 'name', None) or f'成员{getattr(member, "id", "?")}'
            max_points = 0
            for trait in getattr(member, 'traits', None) or []:
                if getattr(trait, 'is_archived', False):
                    continue
                eligible = get_trait_eligible_rest_seconds(
                    member, trait, now=now, tavern_seated=tavern_seated,
                )
                trait_points = int(eligible // cycle_seconds)
                if trait_points <= 0:
                    continue
                max_points = max(max_points, trait_points)
                action = _apply_recovery_to_trait(trait, recovery_points=trait_points)
                if action:
                    _reset_trait_rest_baseline_after_cycle_consumed(trait, trait_points * cycle_seconds)
                    events.append(_build_recovery_event(member_name, trait.name, action))
            points = max_points

        if live_settlement:
            reset_trait_recovery_rest_tracking(member)
    return events


def settle_due_trace_trait_recovery(
    members: Iterable,
    *,
    now: datetime | None = None,
) -> List[str]:
    """Archive trace-stage traits once wall-clock expiry is reached."""
    events: List[str] = []
    now = now or _portable_utc_now()
    for member in members:
        member_name = getattr(member, 'name', None) or f'成员{getattr(member, "id", "?")}'
        for trait in getattr(member, 'traits', None) or []:
            if getattr(trait, 'is_archived', False):
                continue
            if (getattr(trait, 'recovery_stage', None) or '').strip().lower() != 'trace':
                continue
            expires_at = getattr(trait, 'recovery_expires_at', None)
            if expires_at is None:
                hours = _coerce_trace_hours(getattr(trait, 'recovery_hours', None))
                base = getattr(trait, 'updated_at', None) or getattr(trait, 'created_at', None) or now
                expires_at = base + timedelta(hours=hours)
                trait.recovery_expires_at = expires_at
            if now >= expires_at:
                _archive_trait(trait, reason='痕迹自然消退')
                events.append(f'{member_name}的「{trait.name}」已自行消退。')
    return events


def settle_due_natural_trait_recovery(
    members: Iterable,
    *,
    now: datetime | None = None,
) -> List[str]:
    """Settle completed dorm-bed recovery cycles without requiring departure.

    This is used by scheduler/state-transition write paths so a trait is persisted
    once its backend-computed recovery cycle is due. Partial cycle remainder is
    preserved; unlike expedition departure, this does not wipe unfinished rest.
    """
    events: List[str] = []
    member_list = list(members)
    if not member_list:
        return events

    from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
        AdventurerS1TavernSeatService,
    )
    seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(
        [getattr(m, 'id', None) for m in member_list if getattr(m, 'id', None)],
    )

    now = now or _portable_utc_now()
    cycle_seconds = _configured_cycle_hours() * 3600
    for member in member_list:
        tavern_seated = getattr(member, 'id', None) in seated_ids
        total_seconds = get_trait_recovery_total_rest_seconds(
            member, now=now, tavern_seated=tavern_seated,
        )
        if total_seconds <= 0 and not any(getattr(trait, 'recovery_rest_baseline_persistent', False) for trait in getattr(member, 'traits', []) or []):
            continue

        member_name = getattr(member, 'name', None) or f'成员{getattr(member, "id", "?")}'
        changed = False
        max_remaining_eligible = 0
        for trait in getattr(member, 'traits', None) or []:
            if getattr(trait, 'is_archived', False):
                continue
            eligible = get_trait_eligible_rest_seconds(
                member, trait, now=now, tavern_seated=tavern_seated,
            )
            trait_points = int(eligible // cycle_seconds)
            if trait_points <= 0:
                continue
            action = _apply_recovery_to_trait(trait, recovery_points=trait_points)
            if action:
                changed = True
                consumed_seconds = trait_points * cycle_seconds
                max_remaining_eligible = max(
                    max_remaining_eligible,
                    max(0, eligible - consumed_seconds),
                )
                _reset_trait_rest_baseline_after_cycle_consumed(trait, trait_points * cycle_seconds)
                events.append(_build_recovery_event(member_name, trait.name, action))

        if changed:
            _finalize_member_rest_after_cycle_settlement(
                member,
                remaining_eligible_seconds=max_remaining_eligible,
                now=now,
                tavern_seated=tavern_seated,
            )

    return events


def _bounded_trait_recovery_config(
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = _coerce_int(current_app.config.get(key, default), default)
    return max(minimum, min(maximum, value))


def _trait_recovery_batch_size() -> int:
    return _bounded_trait_recovery_config(
        'ADVENTURER_S1_TRAIT_RECOVERY_SETTLE_BATCH_SIZE',
        100,
        minimum=1,
        maximum=1000,
    )


def _trait_recovery_max_batches_per_run() -> int:
    return _bounded_trait_recovery_config(
        'ADVENTURER_S1_TRAIT_RECOVERY_SETTLE_MAX_BATCHES_PER_RUN',
        4,
        minimum=1,
        maximum=20,
    )


def _trait_recovery_startup_max_batches() -> int:
    return _bounded_trait_recovery_config(
        'ADVENTURER_S1_TRAIT_RECOVERY_STARTUP_MAX_BATCHES',
        1,
        minimum=1,
        maximum=20,
    )


def _trait_recovery_batch_state() -> dict:
    """Process-local fairness cursor; database rows remain the sole authority."""
    key = 'adventurer_s1_trait_recovery_batch_state'
    state = current_app.extensions.get(key)
    if not isinstance(state, dict):
        state = {'after_adventurer_id': 0}
        current_app.extensions[key] = state
    return state


def collect_trait_recovery_settlement_candidates(
    *,
    now: datetime | None = None,
    after_adventurer_id: int = 0,
    batch_size: int = 100,
    lock_rows: bool = False,
) -> list:
    """Load a stable candidate window, optionally locking parents for settlement."""
    from sqlalchemy import and_, or_
    from sqlalchemy.orm import joinedload, noload, selectinload

    from plugins.adventurer_s1.models import (
        AdventurerS1,
        AdventurerS1BuildingInstance,
        AdventurerS1DormRoom,
        AdventurerS1Trait,
    )

    now = now or _portable_utc_now()
    candidate_exists = (
        AdventurerS1Trait.query.filter(
            AdventurerS1Trait.adventurer_id == AdventurerS1.id,
            AdventurerS1Trait.is_archived.is_(False),
            AdventurerS1Trait.recovery_policy.in_(tuple(NATURAL_RECOVERY_POLICIES)),
            or_(
                AdventurerS1Trait.recovery_stage.in_(tuple(DORM_RECOVERY_STAGES)),
                and_(
                    AdventurerS1Trait.recovery_stage == 'trace',
                    or_(
                        AdventurerS1Trait.recovery_expires_at.is_(None),
                        AdventurerS1Trait.recovery_expires_at <= now,
                    ),
                ),
            ),
        ).exists()
    )
    query = (
        AdventurerS1.query.options(
            noload('*'),
            selectinload(AdventurerS1.traits),
            selectinload(AdventurerS1.building_instance).joinedload(
                AdventurerS1BuildingInstance.template,
            ),
            selectinload(AdventurerS1.dorm_room).joinedload(
                AdventurerS1DormRoom.building_instance,
            ).joinedload(
                AdventurerS1BuildingInstance.template,
            ),
        )
        .filter(
            AdventurerS1.id > max(0, int(after_adventurer_id or 0)),
            AdventurerS1.status.notin_(('dead', 'archived')),
            candidate_exists,
        )
        .order_by(AdventurerS1.id.asc())
        .populate_existing()
    )
    query = query.limit(min(1000, max(1, int(batch_size))))
    if lock_rows:
        query = query.with_for_update(skip_locked=True)
    return query.all()


def _count_active_natural_recovery_traits(adventurers: list) -> int:
    return sum(
        1
        for adv in adventurers
        for trait in getattr(adv, 'traits', None) or []
        if not getattr(trait, 'is_archived', False)
        and trait_has_natural_dorm_recovery(trait)
    )


def _clear_stale_recovery_baseline_batch(batch_size: int) -> int:
    """Clear one idempotent legacy batch and release its connection immediately."""
    from db_repository import db_manager
    from plugins.adventurer_s1.models import AdventurerS1Trait

    try:
        stale = (
            AdventurerS1Trait.query.filter(
                AdventurerS1Trait.is_archived.is_(False),
                AdventurerS1Trait.recovery_progress > 0,
                AdventurerS1Trait.recovery_rest_baseline_seconds.isnot(None),
                AdventurerS1Trait.recovery_rest_baseline_persistent.is_(False),
            )
            .order_by(AdventurerS1Trait.id.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
            .all()
        )
        if not stale:
            db_manager.rollback()
            return 0
        for trait in stale:
            trait.recovery_rest_baseline_seconds = None
        committed, commit_error = db_manager.commit()
        if not committed:
            raise RuntimeError(
                commit_error or '提交特质恢复旧基线修复失败'
            )
        return len(stale)
    except Exception:
        db_manager.rollback()
        raise
    finally:
        db_manager.remove()


def _settle_trait_recovery_candidate_batch(
    *,
    after_adventurer_id: int,
    batch_size: int,
    settle_due: bool,
) -> dict:
    """Lock, settle and commit one adventurer window before publishing effects."""
    from db_repository import db_manager
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

    notification = {'notify_ids': [], 'activity_owner_ids': []}
    try:
        now = _portable_utc_now()
        candidates = collect_trait_recovery_settlement_candidates(
            now=now,
            after_adventurer_id=after_adventurer_id,
            batch_size=batch_size,
            lock_rows=True,
        )
        if not candidates:
            db_manager.rollback()
            return {
                'candidate_adventurers': 0,
                'cleared_inflated_baselines': 0,
                'settled_traits': 0,
                'changed_adventurers': 0,
                'last_adventurer_id': after_adventurer_id,
                'reached_end': True,
            }

        cleared_inflated_baselines = 0
        baseline_repaired_adventurer_ids: set[int] = set()
        for adv in candidates:
            for trait in getattr(adv, 'traits', None) or []:
                if getattr(trait, 'is_archived', False):
                    continue
                before = getattr(trait, 'recovery_rest_baseline_seconds', None)
                _repair_recovery_rest_baseline(trait, adv, now=now)
                if before is not None and trait.recovery_rest_baseline_seconds is None:
                    cleared_inflated_baselines += 1
                    baseline_repaired_adventurer_ids.add(int(adv.id))

        active_before = _count_active_natural_recovery_traits(candidates)
        if settle_due:
            notification = AdventurerS1Service._settle_due_trait_recovery_for_adventurers(
                candidates,
                auto_commit=False,
                notify=False,
            )
        changed_adventurer_ids = set(notification.get('notify_ids') or [])
        notification['notify_ids'] = sorted(
            changed_adventurer_ids | baseline_repaired_adventurer_ids
        )
        active_after = _count_active_natural_recovery_traits(candidates)
        candidate_count = len(candidates)
        last_adventurer_id = int(candidates[-1].id)
        reached_end = candidate_count < batch_size
        committed, commit_error = db_manager.commit()
        if not committed:
            raise RuntimeError(
                commit_error or '提交特质恢复批量结算失败'
            )

        try:
            AdventurerS1Service._publish_trait_recovery_notifications(notification)
        except Exception:
            # The database commit is authoritative. A best-effort cache/socket
            # side effect must never turn a committed batch into a retry error.
            current_app.logger.exception('S1 特质恢复提交后通知失败')
        return {
            'candidate_adventurers': candidate_count,
            'cleared_inflated_baselines': cleared_inflated_baselines,
            'settled_traits': max(0, active_before - active_after),
            'changed_adventurers': len(changed_adventurer_ids),
            'last_adventurer_id': last_adventurer_id,
            'reached_end': reached_end,
        }
    except Exception:
        db_manager.rollback()
        raise
    finally:
        db_manager.remove()


def repair_trait_recovery_state(
    *,
    settle_due: bool = True,
    batch_size: int | None = None,
    max_batches: int | None = None,
) -> dict:
    """Repair and settle bounded short transactions with a fair rotating cursor."""
    requested_batch_size = (
        _trait_recovery_batch_size()
        if batch_size is None
        else int(batch_size)
    )
    effective_batch_size = min(
        1000,
        max(1, requested_batch_size),
    )
    requested_max_batches = (
        _trait_recovery_max_batches_per_run()
        if max_batches is None
        else int(max_batches)
    )
    effective_max_batches = min(
        20,
        max(1, requested_max_batches),
    )
    stats = {
        'cleared_stale_baselines': 0,
        'cleared_inflated_baselines': 0,
        'settled_traits': 0,
        'changed_adventurers': 0,
        'candidate_adventurers': 0,
        'stale_batches': 0,
        'candidate_batches': 0,
        'scan_wrapped': False,
        'next_after_adventurer_id': 0,
        'batch_size': effective_batch_size,
        'max_batches': effective_max_batches,
    }

    remaining_batches = effective_max_batches
    cleared = _clear_stale_recovery_baseline_batch(effective_batch_size)
    stats['cleared_stale_baselines'] = cleared
    if cleared:
        stats['stale_batches'] = 1
        remaining_batches -= 1

    state = _trait_recovery_batch_state()
    after_adventurer_id = max(0, int(state.get('after_adventurer_id') or 0))
    wrapped_once = False
    while remaining_batches > 0:
        batch_stats = _settle_trait_recovery_candidate_batch(
            after_adventurer_id=after_adventurer_id,
            batch_size=effective_batch_size,
            settle_due=settle_due,
        )
        candidate_count = int(batch_stats['candidate_adventurers'])
        if candidate_count <= 0:
            if after_adventurer_id > 0 and not wrapped_once:
                after_adventurer_id = 0
                state['after_adventurer_id'] = 0
                stats['scan_wrapped'] = True
                wrapped_once = True
                if stats['candidate_batches'] == 0:
                    continue
            state['after_adventurer_id'] = 0
            break

        remaining_batches -= 1
        stats['candidate_batches'] += 1
        stats['candidate_adventurers'] += candidate_count
        stats['cleared_inflated_baselines'] += int(
            batch_stats['cleared_inflated_baselines']
        )
        stats['settled_traits'] += int(batch_stats['settled_traits'])
        stats['changed_adventurers'] += int(batch_stats['changed_adventurers'])
        if batch_stats['reached_end']:
            state['after_adventurer_id'] = 0
            stats['scan_wrapped'] = True
            break
        after_adventurer_id = int(batch_stats['last_adventurer_id'])
        state['after_adventurer_id'] = after_adventurer_id

    stats['next_after_adventurer_id'] = max(
        0,
        int(state.get('after_adventurer_id') or 0),
    )
    return stats


def run_startup_trait_recovery_repair() -> dict:
    """Idempotent startup hook: one bounded pass; scheduler continues the scan."""
    return repair_trait_recovery_state(
        settle_due=True,
        batch_size=_trait_recovery_batch_size(),
        max_batches=_trait_recovery_startup_max_batches(),
    )


def tick_due_trait_recovery_job() -> int:
    """APScheduler entry: bounded, resumable recovery settlement and repair."""
    started_at = monotonic()
    batch_size = _trait_recovery_batch_size()
    max_batches = _trait_recovery_max_batches_per_run()
    stats = repair_trait_recovery_state(
        settle_due=True,
        batch_size=batch_size,
        max_batches=max_batches,
    )
    budget_exhausted = (
        int(stats['stale_batches']) + int(stats['candidate_batches'])
        >= max_batches
        and not bool(stats['scan_wrapped'])
    )
    stats['budget_exhausted'] = budget_exhausted
    stats['duration_ms'] = round((monotonic() - started_at) * 1000, 2)
    if (
        int(stats['cleared_stale_baselines']) > 0
        or int(stats['cleared_inflated_baselines']) > 0
        or int(stats['settled_traits']) > 0
        or int(stats['changed_adventurers']) > 0
        or budget_exhausted
    ):
        current_app.logger.info('S1 特质恢复有界结算: %s', stats)
    return int(stats['settled_traits'])


def rebase_transferred_trait_rest(trait, source, target) -> None:
    """Preserve exact eligible rest when a trait changes ownership."""
    if not trait_has_natural_dorm_recovery(trait):
        return
    from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
    seated = AdventurerS1TavernSeatService.get_seated_adventurer_ids([source.id, target.id])
    now = _portable_utc_now()
    eligible = get_trait_eligible_rest_seconds(source, trait, now=now, tavern_seated=source.id in seated)
    target_total = get_trait_recovery_total_rest_seconds(target, now=now, tavern_seated=target.id in seated)
    trait.recovery_rest_baseline_seconds = target_total - eligible
    trait.recovery_rest_baseline_persistent = True
