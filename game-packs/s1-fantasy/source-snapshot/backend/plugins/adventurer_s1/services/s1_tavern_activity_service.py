"""S1 酒馆活动框架与叙事淘汰赛首个玩法。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import math
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app
from sqlalchemy import func

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Activity,
    AdventurerS1ActivityMatch,
    AdventurerS1ActivityParticipant,
    AdventurerS1ActivityRegistration,
    AdventurerS1ExpeditionLog,
)
from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
from plugins.adventurer_s1.services.s1_tavern_activity_ai_service import (
    DEFAULT_GAMEPLAY_CONTRACT_ID,
    PROMPT_VERSION,
    TavernActivityNarrativeError,
    generate_and_review_match,
    get_tavern_activity_gameplay_contract,
)
from plugins.adventurer_s1.services.s1_tavern_poker_engine import (
    resolve_holdem_tournament,
    verify_holdem_tournament_result,
)
from utils.time_utils import to_utc_iso

_ACTIVE_ACTIVITY_STATUSES = ('draft', 'registration', 'running')
_OPEN_MATCH_STATUSES = ('scheduled', 'checkin_open', 'quota_wait')
_STAGE_PENDING_MATCH_STATUSES = (
    'scheduled', 'checkin_open', 'quota_wait', 'generating', 'awaiting_reveal', 'failed',
)


def _append_narrative_audit_run(
    result_data: dict[str, Any],
    *,
    run_number: int,
    outcome: str,
    failure_code: str | None,
    attempt_audit: list[dict[str, Any]],
) -> None:
    raw_limit = _int_config(
        'ADVENTURER_S1_ACTIVITY_AI_AUDIT_RAW_CHAR_LIMIT',
        20000,
    )
    run_limit = _int_config(
        'ADVENTURER_S1_ACTIVITY_AI_AUDIT_RUN_LIMIT',
        8,
    )
    sanitized_attempts = []
    for entry in attempt_audit:
        sanitized = {
            'stage': str(entry.get('stage') or ''),
            'attempt': int(entry.get('attempt') or 0),
            'reason_code': str(entry.get('reason_code') or ''),
        }
        for key in ('candidate_raw_response', 'raw_response'):
            if key in entry:
                sanitized[key] = str(entry.get(key) or '')[:raw_limit]
        sanitized_attempts.append(sanitized)
    runs = list(result_data.get('narrative_attempt_audit') or [])
    runs.append({
        'run_number': max(1, int(run_number)),
        'outcome': outcome,
        'failure_code': str(failure_code or 'none'),
        'attempts': sanitized_attempts,
    })
    result_data['narrative_attempt_audit'] = runs[-run_limit:]


def _int_config(key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _parse_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or '').strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = f'{text[:-1]}+00:00'
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def get_daily_narrative_quota_state() -> tuple[int, int]:
    """主叙事调用共享同一北京时间日配额：普通探险 + 酒馆活动比赛。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_throttle_service import (
        get_beijing_day_start_utc,
    )
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService

    try:
        raw_quota = S1SettingsService.get('daily_expedition_quota')
        quota = int(100 if raw_quota is None else raw_quota)
    except (TypeError, ValueError):
        quota = 100
    day_start = get_beijing_day_start_utc()
    expedition_count = AdventurerS1ExpeditionLog.query.filter(
        AdventurerS1ExpeditionLog.created_at >= day_start,
    ).count()
    activity_count = AdventurerS1ActivityMatch.query.filter(
        AdventurerS1ActivityMatch.ai_requested_at >= day_start,
    ).count()
    return expedition_count + activity_count, quota


def _shared_narrative_quota_is_full() -> bool:
    """活动投递也预留已排队探险的额度，避免两条调度路径合计突破硬上限。"""
    from plugins.adventurer_s1.services.adventurer_s1_expedition_throttle_service import (
        count_queued_departures,
    )

    used, quota = get_daily_narrative_quota_state()
    return quota > 0 and used + count_queued_departures() >= quota


class S1TavernActivityService:
    DEFAULT_ACTIVITY_TYPE = 'narrative_tournament'
    _FROZEN_POLICIES = {
        'settlement_mode': 'ranking_only',
        'appeal_policy': 'disabled',
        'world_injection_policy': 'disabled',
        'review_pipeline': 'activity_local',
    }

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            'gameplay_contract_id': DEFAULT_GAMEPLAY_CONTRACT_ID,
            'group_size': _int_config('ADVENTURER_S1_ACTIVITY_GROUP_SIZE', 8, minimum=2),
            'advance_count': _int_config(
                'ADVENTURER_S1_ACTIVITY_ADVANCE_COUNT', 2, minimum=1,
            ),
            'checkin_minutes': _int_config(
                'ADVENTURER_S1_ACTIVITY_CHECKIN_MINUTES', 60,
            ),
            'stagger_minutes': _int_config(
                'ADVENTURER_S1_ACTIVITY_GROUP_STAGGER_MINUTES', 10,
            ),
            'reveal_minutes': _int_config(
                'ADVENTURER_S1_ACTIVITY_REVEAL_MINUTES', 40,
            ),
            'stamina_cost': _int_config(
                'ADVENTURER_S1_ACTIVITY_STAMINA_COST', 80,
            ),
            'stamina_min_to_enter': _int_config(
                'ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80, minimum=0,
            ),
            'poker_starting_stack': _int_config(
                'ADVENTURER_S1_ACTIVITY_POKER_STARTING_STACK', 1000,
            ),
            'poker_small_blind': _int_config(
                'ADVENTURER_S1_ACTIVITY_POKER_SMALL_BLIND', 10,
            ),
            'poker_big_blind': _int_config(
                'ADVENTURER_S1_ACTIVITY_POKER_BIG_BLIND', 20,
            ),
            'poker_blind_raise_every_hands': _int_config(
                'ADVENTURER_S1_ACTIVITY_POKER_BLIND_RAISE_EVERY_HANDS', 6,
            ),
            'poker_max_hands': _int_config(
                'ADVENTURER_S1_ACTIVITY_POKER_MAX_HANDS', 256,
            ),
            **S1TavernActivityService._FROZEN_POLICIES,
            'draw_seed': secrets.randbits(63),
        }

    @staticmethod
    def _resolve_gameplay_contract(activity: AdventurerS1Activity) -> dict[str, Any]:
        contract_id = str(
            (activity.config_snapshot or {}).get('gameplay_contract_id') or '',
        ).strip()
        contract = get_tavern_activity_gameplay_contract(contract_id)
        if not contract:
            raise RuntimeError('活动冻结玩法契约缺失或不受支持')
        return contract

    @staticmethod
    def _freeze_gameplay_contract(activity: AdventurerS1Activity) -> dict[str, Any]:
        """兼容旧活动：仅在首次开赛事务内补冻结默认玩法，之后拒绝漂移。"""
        config = dict(activity.config_snapshot or {})
        if not str(config.get('gameplay_contract_id') or '').strip():
            config['gameplay_contract_id'] = DEFAULT_GAMEPLAY_CONTRACT_ID
            activity.config_snapshot = config
        return S1TavernActivityService._resolve_gameplay_contract(activity)

    @staticmethod
    def _resolve_match_mechanics(
        activity: AdventurerS1Activity,
        participants: list[AdventurerS1ActivityParticipant],
    ) -> dict[str, Any]:
        """Freeze one complete auditable result before any narrative request."""
        config = dict(activity.config_snapshot or {})
        small_blind = max(1, int(config.get('poker_small_blind', 10)))
        big_blind = max(1, int(config.get('poker_big_blind', 20)))
        if big_blind <= small_blind:
            raise RuntimeError('命运牌桌大盲注必须高于小盲注')
        frozen_players = [
            {
                'adventurer_id': int(participant.adventurer_id),
                'seat': int(participant.draw_slot),
            }
            for participant in participants
        ]
        return resolve_holdem_tournament(
            frozen_players,
            seed=secrets.randbits(63),
            starting_stack=max(1, int(config.get('poker_starting_stack', 1000))),
            small_blind=small_blind,
            big_blind=big_blind,
            blind_raise_every_hands=max(
                1,
                int(config.get('poker_blind_raise_every_hands', 6)),
            ),
            max_hands=max(1, int(config.get('poker_max_hands', 256))),
        )

    @staticmethod
    def _mechanical_result_payload(
        activity: AdventurerS1Activity,
        participants: list[AdventurerS1ActivityParticipant],
        *,
        story_status: str,
    ) -> dict[str, Any]:
        gameplay_contract = S1TavernActivityService._freeze_gameplay_contract(activity)
        return {
            'gameplay_contract_id': gameplay_contract['contract_id'],
            'mechanical': True,
            'resolution_kind': 'holdem_state_machine',
            'mechanical_result': S1TavernActivityService._resolve_match_mechanics(
                activity,
                participants,
            ),
            'story_status': story_status,
        }

    @staticmethod
    def create_activity(body: dict, admin_user_id: int) -> tuple[dict | None, str | None]:
        existing = AdventurerS1Activity.query.filter(
            AdventurerS1Activity.status.in_(_ACTIVE_ACTIVITY_STATUSES),
        ).first()
        if existing:
            return None, '已有未结束的酒馆活动'
        activity_type = str(
            body.get('activity_type') or S1TavernActivityService.DEFAULT_ACTIVITY_TYPE,
        ).strip()
        if activity_type != S1TavernActivityService.DEFAULT_ACTIVITY_TYPE:
            return None, '当前只开放叙事淘汰赛活动类型'
        name = str(body.get('name') or '赌王大赛').strip()[:100]
        if not name:
            return None, '活动名称不能为空'
        first_stage_start_at = _parse_utc_datetime(body.get('first_stage_start_at'))
        config = S1TavernActivityService._default_config()
        overrides = body.get('config')
        if isinstance(overrides, dict):
            for key in (
                'group_size', 'advance_count', 'checkin_minutes',
                'stagger_minutes', 'reveal_minutes', 'stamina_cost',
            ):
                if key in overrides:
                    try:
                        config[key] = max(1, int(overrides[key]))
                    except (TypeError, ValueError):
                        return None, f'config.{key} 必须是正整数'
        if config['group_size'] < 2:
            return None, '每组人数至少为 2'
        if config['advance_count'] >= config['group_size']:
            return None, '晋级人数必须小于每组人数'

        row = AdventurerS1Activity(
            activity_type=activity_type,
            name=name,
            description=str(body.get('description') or '').strip(),
            config_snapshot=config,
            first_stage_start_at=first_stage_start_at,
            created_by_user_id=admin_user_id,
        )
        db_manager.add(row)
        db_manager.commit()
        return S1TavernActivityService._serialize_activity(row, admin=True), None

    @staticmethod
    def open_registration(
        activity_id: int,
        *,
        registration_close_at: object = None,
    ) -> tuple[dict | None, str | None]:
        row = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id)
        if not row:
            return None, '活动不存在'
        if row.status not in ('draft', 'registration'):
            return None, '当前活动状态不能开放报名'
        close_at = _parse_utc_datetime(registration_close_at)
        now = _portable_utc_now()
        if close_at and close_at <= now:
            return None, '报名截止时间必须晚于当前时间'
        row.status = 'registration'
        row.registration_open_at = row.registration_open_at or now
        row.registration_close_at = close_at
        db_manager.commit()
        return S1TavernActivityService._serialize_activity(row, admin=True), None

    @staticmethod
    def register(
        activity_id: int,
        adventurer_id: int,
        user_id: int,
    ) -> tuple[dict | None, str | None]:
        now = _portable_utc_now()
        with db_manager.transaction():
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id, with_for_update=True)
            if not activity or activity.status != 'registration':
                return None, '活动当前不在报名阶段'
            if activity.registration_close_at and now >= activity.registration_close_at:
                return None, '活动报名已经截止'
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, with_for_update=True)
            if not adv or adv.owner_id != user_id or not adv.is_player_created:
                return None, '只能使用自己的玩家冒险者报名'
            if adv.status in AdventurerS1.TAVERN_EXCLUDED_STATUSES or int(adv.hp or 0) <= 0:
                return None, '死亡或封存的冒险者不能报名'
            exists = AdventurerS1ActivityRegistration.query.filter_by(
                activity_id=activity_id,
                owner_user_id=user_id,
            ).first()
            if exists:
                return None, '每位玩家每期活动只能报名一名冒险者'
            registration = AdventurerS1ActivityRegistration(
                activity_id=activity_id,
                adventurer_id=adv.id,
                owner_user_id=adv.owner_id,
                adventurer_name=adv.name,
            )
            db_manager.add(registration)
        return {'registered': True, 'activity_id': activity_id}, None

    @staticmethod
    def withdraw(
        activity_id: int,
        adventurer_id: int,
        user_id: int,
    ) -> tuple[dict | None, str | None]:
        with db_manager.transaction():
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id, with_for_update=True)
            if not activity or activity.status != 'registration':
                return None, '只有报名阶段可以取消报名'
            registration = AdventurerS1ActivityRegistration.query.filter_by(
                activity_id=activity_id,
                adventurer_id=adventurer_id,
            ).with_for_update().first()
            if not registration or registration.owner_user_id != user_id:
                return None, '没有找到你的报名记录'
            AdventurerS1ActivityRegistration.query.filter_by(id=registration.id).delete(
                synchronize_session=False,
            )
        return {'registered': False, 'activity_id': activity_id}, None

    @staticmethod
    def draw_first_stage(
        activity_id: int,
        *,
        first_stage_start_at: object = None,
    ) -> tuple[dict | None, str | None]:
        now = _portable_utc_now()
        with db_manager.transaction():
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id, with_for_update=True)
            if not activity or activity.status != 'registration':
                return None, '活动当前不能抽签'
            registrations = (
                AdventurerS1ActivityRegistration.query
                .filter_by(activity_id=activity_id, status='registered')
                .order_by(AdventurerS1ActivityRegistration.id)
                .all()
            )
            if len(registrations) < 2:
                return None, '至少需要 2 名参赛者才能抽签'
            start_at = (
                _parse_utc_datetime(first_stage_start_at)
                or activity.first_stage_start_at
            )
            checkin_minutes = int((activity.config_snapshot or {}).get('checkin_minutes', 60))
            if not start_at or start_at <= now + timedelta(minutes=checkin_minutes):
                return None, f'首组开赛时间至少需晚于当前时间 {checkin_minutes} 分钟'
            activity.first_stage_start_at = start_at
            activity.registration_close_at = now
            activity.status = 'running'
            S1TavernActivityService._create_stage_locked(
                activity, registrations, stage_number=1, first_group_start_at=start_at,
            )
        return S1TavernActivityService.get_activity(activity_id, admin=True), None

    @staticmethod
    def _create_stage_locked(
        activity: AdventurerS1Activity,
        registrations: list[AdventurerS1ActivityRegistration],
        *,
        stage_number: int,
        first_group_start_at: datetime,
    ) -> None:
        cfg = dict(activity.config_snapshot or {})
        group_size = max(2, int(cfg.get('group_size', 8)))
        group_count = max(1, math.ceil(len(registrations) / group_size))
        shuffled = list(registrations)
        seed = int(cfg.get('draw_seed') or 0) + stage_number * 1_000_003
        random.Random(seed).shuffle(shuffled)
        groups: list[list[AdventurerS1ActivityRegistration]] = [
            [] for _ in range(group_count)
        ]
        for index, registration in enumerate(shuffled):
            groups[index % group_count].append(registration)

        is_final = len(registrations) <= group_size
        stagger = int(cfg.get('stagger_minutes', 10))
        checkin = int(cfg.get('checkin_minutes', 60))
        reveal = int(cfg.get('reveal_minutes', 40))
        adventurer_ids = [registration.adventurer_id for registration in registrations]
        adventurers = {
            row.id: row
            for row in AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids)).all()
        }
        for group_index, group in enumerate(groups, start=1):
            starts_at = first_group_start_at + timedelta(minutes=(group_index - 1) * stagger)
            match = AdventurerS1ActivityMatch(
                activity_id=activity.id,
                stage_number=stage_number,
                group_number=group_index,
                is_final=is_final,
                status='scheduled',
                checkin_open_at=starts_at - timedelta(minutes=checkin),
                starts_at=starts_at,
                reveal_at=starts_at + timedelta(minutes=reveal),
            )
            db_manager.add(match)
            db_manager.flush()
            for draw_slot, registration in enumerate(group, start=1):
                adv = adventurers.get(registration.adventurer_id)
                participant = AdventurerS1ActivityParticipant(
                    match_id=match.id,
                    registration_id=registration.id,
                    adventurer_id=registration.adventurer_id,
                    owner_user_id=registration.owner_user_id,
                    adventurer_name=registration.adventurer_name,
                    personality_snapshot=str(getattr(adv, 'personality', '') or '')[:200],
                    draw_slot=draw_slot,
                )
                db_manager.add(participant)
                registration.status = 'active'
        activity.current_stage = stage_number

    @staticmethod
    def check_in(
        match_id: int,
        adventurer_id: int,
        user_id: int,
    ) -> tuple[dict | None, str | None]:
        now = _portable_utc_now()
        with db_manager.transaction():
            match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
            if not match or match.status not in _OPEN_MATCH_STATUSES:
                return None, '该组当前不能签到'
            if now < match.checkin_open_at:
                return None, '该组签到尚未开放'
            if now >= match.starts_at:
                return None, '该组签到已经截止'
            participant = AdventurerS1ActivityParticipant.query.filter_by(
                match_id=match_id,
                adventurer_id=adventurer_id,
            ).with_for_update().first()
            if not participant or participant.owner_user_id != user_id:
                return None, '你不在该固定分组中'
            if participant.status == 'checked_in':
                return {'checked_in': True, 'match_id': match_id}, None
            if participant.status != 'assigned':
                return None, '当前参赛状态不能签到'
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, with_for_update=True)
            if not adv or adv.owner_id != user_id:
                return None, '冒险者不存在或归属已变化'
            ok, reason = adv.can_dispatch()
            if not ok:
                return None, reason or '冒险者当前正忙'
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                get_live_stamina,
                s1_max_stamina,
                s1_stamina_recovery_per_minute,
            )
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                AdventurerS1TavernSeatService,
            )
            seated = bool(AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id]))
            live_stamina = get_live_stamina(
                adv,
                max_stamina=s1_max_stamina(),
                recovery_per_minute=s1_stamina_recovery_per_minute(),
                now=now,
                tavern_seated=seated,
            )
            minimum = int(
                (AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id).config_snapshot or {})
                .get('stamina_min_to_enter', 80)
            )
            if int(live_stamina) <= minimum:
                return None, f'精力必须高于 {minimum} 才能签到'
            AdventurerS1Service.leave_rest(adv, 'activity', now=now)
            participant.status = 'checked_in'
            participant.checked_in_at = now
            match.status = 'checkin_open'
        return {'checked_in': True, 'match_id': match_id}, None

    @staticmethod
    def _submit_match_task(match_id: int, *, force_retry: bool = False) -> bool:
        from services.async_task_service import AsyncTaskService

        payload = {'match_id': int(match_id)}
        if force_retry:
            key = AsyncTaskService._build_task_dedupe_key('ai_s1_tavern_activity', payload)
            AsyncTaskService._release_task_dedupe_key(key)
        elif AsyncTaskService.is_task_enqueued_or_running('ai_s1_tavern_activity', payload):
            return True
        return bool(AsyncTaskService.submit_task('ai_s1_tavern_activity', payload))

    @staticmethod
    def _mark_enqueue_failed(
        match_id: int,
        *,
        restore_ai_requested_at: datetime | None = None,
    ) -> None:
        """Narrative enqueue failure never invalidates an already frozen result."""
        with db_manager.transaction():
            match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
            result_data = dict(match.result_data or {}) if match else {}
            if (
                not match
                or match.status not in ('awaiting_reveal', 'settled')
                or result_data.get('story_status') != 'generating'
            ):
                return
            result_data['story_status'] = 'failed'
            match.result_data = result_data
            match.ai_requested_at = restore_ai_requested_at
            match.generation_finished_at = _portable_utc_now()
            match.last_error = '比赛剧情任务入队失败，请由管理员重试'

    @staticmethod
    def _auto_check_in_open_matches(now: datetime) -> int:
        """按组批量自动签到；只处理签到窗内仍为 assigned 的休息中冒险者。"""
        has_assigned_participant = (
            AdventurerS1ActivityParticipant.query.with_entities(
                AdventurerS1ActivityParticipant.id,
            ).filter(
                AdventurerS1ActivityParticipant.match_id
                == AdventurerS1ActivityMatch.id,
                AdventurerS1ActivityParticipant.status == 'assigned',
            ).exists()
        )
        match_ids = [
            int(row.id)
            for row in AdventurerS1ActivityMatch.query.with_entities(
                AdventurerS1ActivityMatch.id,
            ).filter(
                AdventurerS1ActivityMatch.status == 'checkin_open',
                AdventurerS1ActivityMatch.checkin_open_at <= now,
                AdventurerS1ActivityMatch.starts_at > now,
                has_assigned_participant,
            ).order_by(AdventurerS1ActivityMatch.id).all()
        ]
        checked_in_count = 0
        for match_id in match_ids:
            with db_manager.transaction():
                match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                if (
                    not match
                    or match.status != 'checkin_open'
                    or now < match.checkin_open_at
                    or now >= match.starts_at
                ):
                    continue
                participants = (
                    AdventurerS1ActivityParticipant.query
                    .filter_by(match_id=match.id, status='assigned')
                    .order_by(AdventurerS1ActivityParticipant.draw_slot)
                    .with_for_update().all()
                )
                if not participants:
                    continue
                adventurers = {
                    int(row.id): row
                    for row in AdventurerS1.query.filter(
                        AdventurerS1.id.in_([
                            participant.adventurer_id for participant in participants
                        ]),
                    ).order_by(AdventurerS1.id).with_for_update().all()
                }
                activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id)
                if not activity:
                    continue
                minimum = int(
                    (activity.config_snapshot or {}).get('stamina_min_to_enter', 80),
                )
                from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                    get_live_stamina,
                    s1_max_stamina,
                    s1_stamina_recovery_per_minute,
                )
                from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                    AdventurerS1TavernSeatService,
                )
                candidate_ids = [
                    participant.adventurer_id
                    for participant in participants
                    if participant.adventurer_id in adventurers
                ]
                tavern_seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(
                    candidate_ids,
                )
                participant_by_adventurer_id = {
                    int(participant.adventurer_id): participant
                    for participant in participants
                }
                max_stamina = s1_max_stamina()
                recovery_per_minute = s1_stamina_recovery_per_minute()
                eligible: list[AdventurerS1] = []
                for adventurer_id, adv in adventurers.items():
                    participant = participant_by_adventurer_id.get(adventurer_id)
                    if (
                        not participant
                        or adv.owner_id != participant.owner_user_id
                        or not adv.is_player_created
                        or adv.status != 'resting'
                        or int(adv.hp or 0) <= 0
                    ):
                        continue
                    live_stamina = get_live_stamina(
                        adv,
                        max_stamina=max_stamina,
                        recovery_per_minute=recovery_per_minute,
                        now=now,
                        tavern_seated=adventurer_id in tavern_seated_ids,
                    )
                    if int(live_stamina) <= minimum:
                        continue
                    eligible.append(adv)
                if not eligible:
                    continue
                AdventurerS1Service.leave_rest_batch(
                    eligible,
                    'activity',
                    now=now,
                    tavern_seated_ids=tavern_seated_ids,
                )
                for adv in eligible:
                    participant = participant_by_adventurer_id[int(adv.id)]
                    participant.status = 'checked_in'
                    participant.checked_in_at = now
                checked_in_count += len(eligible)
        return checked_in_count

    @staticmethod
    def tick() -> dict[str, int]:
        """每分钟推进签到窗、开赛、AI 投递、揭晓结算及下一日抽签。"""
        now = _portable_utc_now()
        stats = {
            'opened': 0,
            'auto_checked_in': 0,
            'started': 0,
            'queued': 0,
            'failed': 0,
            'settled': 0,
        }
        stuck_seconds = _int_config(
            'ADVENTURER_S1_ACTIVITY_GENERATION_STUCK_SECONDS', 3600,
        )
        legacy_generating_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter_by(
                status='generating',
            ).all()
            if not (row.result_data or {}).get('mechanical')
        ]
        for match_id in legacy_generating_ids:
            should_queue = False
            with db_manager.transaction():
                match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                if (
                    not match
                    or match.status != 'generating'
                    or (match.result_data or {}).get('mechanical')
                ):
                    continue
                activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity,
                    match.activity_id, with_for_update=True,
                )
                participants = (
                    AdventurerS1ActivityParticipant.query
                    .filter_by(match_id=match.id, status='checked_in')
                    .order_by(AdventurerS1ActivityParticipant.draw_slot)
                    .with_for_update().all()
                )
                if not activity or len(participants) < 2:
                    match.status = 'failed'
                    match.last_error = '旧版活动任务无法恢复机械赛果'
                    stats['failed'] += 1
                    continue
                try:
                    match.result_data = S1TavernActivityService._mechanical_result_payload(
                        activity,
                        participants,
                        story_status='generating',
                    )
                except (TypeError, ValueError, RuntimeError) as exc:
                    match.status = 'failed'
                    match.last_error = f'旧版活动任务机械恢复失败：{str(exc)[:900]}'
                    stats['failed'] += 1
                    continue
                match.status = 'awaiting_reveal'
                match.prompt_version = PROMPT_VERSION
                match.generation_started_at = now
                match.generation_finished_at = None
                match.last_error = None
                should_queue = True
            if should_queue:
                if S1TavernActivityService._submit_match_task(match_id):
                    stats['queued'] += 1
                else:
                    S1TavernActivityService._mark_enqueue_failed(match_id)

        stale_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.status.in_(
                    ('generating', 'awaiting_reveal', 'settled'),
                ),
                AdventurerS1ActivityMatch.generation_started_at <= (
                    now - timedelta(seconds=stuck_seconds)
                ),
                AdventurerS1ActivityMatch.generation_finished_at.is_(None),
            ).all()
            if (row.result_data or {}).get('story_status') == 'generating'
        ]
        for match_id in stale_ids:
            with db_manager.transaction():
                match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                result_data = dict(match.result_data or {}) if match else {}
                if (
                    match
                    and result_data.get('story_status') == 'generating'
                    and match.generation_started_at
                    and match.generation_started_at <= now - timedelta(seconds=stuck_seconds)
                ):
                    result_data['story_status'] = 'failed'
                    match.result_data = result_data
                    match.generation_finished_at = now
                    match.last_error = '比赛剧情生成超时，请由管理员重试'
                    stats['failed'] += 1
        scheduled_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.status == 'scheduled',
                AdventurerS1ActivityMatch.checkin_open_at <= now,
            ).all()
        ]
        if scheduled_ids:
            AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.id.in_(scheduled_ids),
                AdventurerS1ActivityMatch.status == 'scheduled',
            ).update({'status': 'checkin_open'}, synchronize_session=False)
            db_manager.commit()
            stats['opened'] = len(scheduled_ids)

        stats['auto_checked_in'] = S1TavernActivityService._auto_check_in_open_matches(now)

        due_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.status.in_(_OPEN_MATCH_STATUSES),
                AdventurerS1ActivityMatch.starts_at <= now,
            ).order_by(AdventurerS1ActivityMatch.starts_at, AdventurerS1ActivityMatch.id).all()
        ]
        for match_id in due_ids:
            should_queue = False
            with db_manager.transaction():
                match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                if not match or match.status not in _OPEN_MATCH_STATUSES:
                    continue
                participants = (
                    AdventurerS1ActivityParticipant.query
                    .filter_by(match_id=match.id)
                    .order_by(AdventurerS1ActivityParticipant.draw_slot)
                    .with_for_update()
                    .all()
                )
                adv_ids = [p.adventurer_id for p in participants if p.status == 'checked_in']
                adventurers = {
                    row.id: row
                    for row in AdventurerS1.query.filter(AdventurerS1.id.in_(adv_ids))
                    .with_for_update().all()
                } if adv_ids else {}
                active: list[AdventurerS1ActivityParticipant] = []
                activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id, with_for_update=True)
                if not activity:
                    match.status = 'failed'
                    match.last_error = '活动不存在，无法取得冻结玩法契约'
                    stats['failed'] += 1
                    continue
                try:
                    gameplay_contract = S1TavernActivityService._freeze_gameplay_contract(activity)
                except RuntimeError as exc:
                    match.status = 'failed'
                    match.last_error = str(exc)
                    stats['failed'] += 1
                    continue
                stamina_cost = int((activity.config_snapshot or {}).get('stamina_cost', 80))
                for participant in participants:
                    adv = adventurers.get(participant.adventurer_id)
                    if participant.status != 'checked_in':
                        participant.status = 'forfeit'
                        participant.forfeit_reason = '未在签到窗口内进入编号组'
                        continue
                    if not adv or adv.status != 'activity' or int(adv.hp or 0) <= 0:
                        participant.status = 'forfeit'
                        participant.forfeit_reason = '开赛时已死亡或不再具备参赛状态'
                        if adv and adv.status == 'activity':
                            AdventurerS1Service.enter_rest(adv, now=now)
                        continue
                    if not participant.stamina_charged and int(adv.stamina or 0) < stamina_cost:
                        participant.status = 'forfeit'
                        participant.forfeit_reason = '开赛时精力不足'
                        AdventurerS1Service.enter_rest(adv, now=now)
                        continue
                    if not participant.stamina_charged:
                        adv.stamina = int(adv.stamina or 0) - stamina_cost
                        adv.stamina_updated_at = now
                        participant.stamina_charged = stamina_cost
                    active.append(participant)

                if len(active) <= 1:
                    ranking = []
                    if active:
                        ranking.append({
                            'adventurer_id': active[0].adventurer_id,
                            'rank': 1,
                            'result_note': '其余参赛者弃权，自动取得本组第一名。',
                        })
                    match.result_data = {
                        'gameplay_contract_id': gameplay_contract['contract_id'],
                        'ranking': ranking,
                        'mechanical': True,
                        'resolution_kind': 'forfeit_walkover',
                        'story_status': 'complete',
                    }
                    match.story = (
                        f'{active[0].adventurer_name}按时进入赛场；'
                        '其余参赛者均未取得有效参赛资格，本组不经额外裁决直接结束。'
                        if active else '本组无人按时取得有效参赛资格，比赛以全员弃权结束。'
                    )
                    match.status = 'awaiting_reveal'
                    match.generation_finished_at = now
                else:
                    try:
                        mechanical_result = S1TavernActivityService._resolve_match_mechanics(
                            activity,
                            active,
                        )
                    except (TypeError, ValueError, RuntimeError) as exc:
                        match.status = 'failed'
                        match.last_error = f'命运牌桌机械裁决失败：{str(exc)[:900]}'
                        stats['failed'] += 1
                        continue
                    quota_wait = _shared_narrative_quota_is_full()
                    match.result_data = {
                        'gameplay_contract_id': gameplay_contract['contract_id'],
                        'mechanical': True,
                        'resolution_kind': 'holdem_state_machine',
                        'mechanical_result': mechanical_result,
                        'story_status': 'quota_wait' if quota_wait else 'generating',
                    }
                    match.status = 'awaiting_reveal'
                    match.prompt_version = PROMPT_VERSION
                    if quota_wait:
                        match.last_error = '赛果已冻结；故事等待下一叙事配额日补写'
                    else:
                        match.ai_requested_at = match.ai_requested_at or now
                        match.generation_started_at = now
                        match.generation_finished_at = None
                        match.generation_attempts = int(match.generation_attempts or 0) + 1
                        match.last_error = None
                        should_queue = True
                stats['started'] += 1
            if should_queue:
                if S1TavernActivityService._submit_match_task(match_id):
                    stats['queued'] += 1
                else:
                    S1TavernActivityService._mark_enqueue_failed(match_id)

        narrative_waiting_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.status.in_(('awaiting_reveal', 'settled')),
                AdventurerS1ActivityMatch.prompt_version == PROMPT_VERSION,
                AdventurerS1ActivityMatch.ai_requested_at.is_(None),
                AdventurerS1ActivityMatch.generation_started_at.is_(None),
                AdventurerS1ActivityMatch.generation_finished_at.is_(None),
                AdventurerS1ActivityMatch.story.is_(None),
            ).all()
            if (row.result_data or {}).get('story_status') == 'quota_wait'
        ]
        for match_id in narrative_waiting_ids:
            if _shared_narrative_quota_is_full():
                break
            with db_manager.transaction():
                match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                result_data = dict(match.result_data or {}) if match else {}
                if not match or result_data.get('story_status') != 'quota_wait':
                    continue
                result_data['story_status'] = 'generating'
                match.result_data = result_data
                match.ai_requested_at = now
                match.generation_started_at = now
                match.generation_finished_at = None
                match.generation_attempts = int(match.generation_attempts or 0) + 1
                match.prompt_version = PROMPT_VERSION
                match.last_error = None
            if S1TavernActivityService._submit_match_task(match_id):
                stats['queued'] += 1
            else:
                S1TavernActivityService._mark_enqueue_failed(match_id)

        reveal_ids = [
            row.id for row in AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.status == 'awaiting_reveal',
                AdventurerS1ActivityMatch.reveal_at <= now,
            ).all()
        ]
        for match_id in reveal_ids:
            if S1TavernActivityService._settle_match(match_id, now=now):
                stats['settled'] += 1

        # 结算事务与“创建下一阶段”故意分开；若进程恰在两者之间退出，
        # 后续 tick 仍会从已持久化的 settled 组恢复阶段推进。
        running_stages = [
            (row.id, row.current_stage)
            for row in AdventurerS1Activity.query.filter_by(status='running').all()
            if int(row.current_stage or 0) > 0
        ]
        for activity_id, stage_number in running_stages:
            S1TavernActivityService._finish_stage_if_ready(
                int(activity_id), int(stage_number), now=now,
            )
        return stats

    @staticmethod
    def process_match_ai(match_id: int) -> None:
        match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id)
        result_data = dict(match.result_data or {}) if match else {}
        if (
            not match
            or match.status not in ('awaiting_reveal', 'settled')
            or result_data.get('story_status') != 'generating'
        ):
            return
        activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id)
        ranking = (result_data.get('mechanical_result') or {}).get('ranking') or []
        mechanical_participant_ids = {
            int(row['adventurer_id'])
            for row in ranking
            if isinstance(row, dict) and row.get('adventurer_id') is not None
        }
        participants = (
            AdventurerS1ActivityParticipant.query
            .filter(
                AdventurerS1ActivityParticipant.match_id == match.id,
                AdventurerS1ActivityParticipant.adventurer_id.in_(
                    mechanical_participant_ids,
                ),
            )
            .order_by(AdventurerS1ActivityParticipant.draw_slot)
            .all()
        )
        frozen = [
            {
                'adventurer_id': p.adventurer_id,
                'name': p.adventurer_name,
                'personality': p.personality_snapshot,
            }
            for p in participants
        ]
        run_number = max(1, int(match.generation_attempts or 1))
        try:
            gameplay_contract = S1TavernActivityService._resolve_gameplay_contract(activity)
            mechanical_result = verify_holdem_tournament_result(
                result_data.get('mechanical_result'),
                frozen,
            )
            if mechanical_result is None:
                raise RuntimeError('冻结机械赛果未通过重放校验')
            result, raw, review, review_raw, attempt_audit = generate_and_review_match(
                match_id=match.id,
                activity_name=activity.name,
                stage_number=match.stage_number,
                group_number=match.group_number,
                is_final=bool(match.is_final),
                participants=frozen,
                gameplay_contract=gameplay_contract,
                mechanical_result=mechanical_result,
            )
        except TavernActivityNarrativeError as exc:
            db_manager.remove()
            with db_manager.transaction():
                locked = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                locked_result = dict(locked.result_data or {}) if locked else {}
                if (
                    locked
                    and locked.status in ('awaiting_reveal', 'settled')
                    and locked_result.get('story_status') == 'generating'
                ):
                    _append_narrative_audit_run(
                        locked_result,
                        run_number=run_number,
                        outcome='failed',
                        failure_code=exc.failure_code,
                        attempt_audit=exc.attempt_audit,
                    )
                    locked_result['story_status'] = 'failed'
                    locked.result_data = locked_result
                    locked.generation_finished_at = _portable_utc_now()
                    locked.last_error = str(exc)[:1000]
            current_app.logger.warning(
                'S1 酒馆活动叙事合同失败 match=%s code=%s',
                match_id,
                exc.failure_code,
            )
            return
        except Exception as exc:
            db_manager.remove()
            with db_manager.transaction():
                locked = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
                locked_result = dict(locked.result_data or {}) if locked else {}
                if (
                    locked
                    and locked.status in ('awaiting_reveal', 'settled')
                    and locked_result.get('story_status') == 'generating'
                ):
                    locked_result['story_status'] = 'failed'
                    locked.result_data = locked_result
                    locked.generation_finished_at = _portable_utc_now()
                    locked.last_error = str(exc)[:1000]
            current_app.logger.exception('S1 酒馆活动 AI 失败 match=%s', match_id)
            return

        db_manager.remove()
        with db_manager.transaction():
            locked = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
            locked_result = dict(locked.result_data or {}) if locked else {}
            if (
                not locked
                or locked.status not in ('awaiting_reveal', 'settled')
                or locked_result.get('story_status') != 'generating'
            ):
                return
            _append_narrative_audit_run(
                locked_result,
                run_number=run_number,
                outcome='complete',
                failure_code=None,
                attempt_audit=attempt_audit,
            )
            locked_result['story_status'] = 'complete'
            locked.result_data = locked_result
            locked.story = result['story']
            locked.raw_response = raw
            locked.review_data = review
            locked.review_raw_response = review_raw
            locked.generation_finished_at = _portable_utc_now()
            locked.last_error = None

    @staticmethod
    def retry_match(match_id: int) -> tuple[dict | None, str | None]:
        now = _portable_utc_now()
        previous_ai_requested_at = None
        with db_manager.transaction():
            match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
            result_data = dict(match.result_data or {}) if match else {}
            if (
                not match
                or match.status not in ('awaiting_reveal', 'settled')
                or result_data.get('story_status') != 'failed'
            ):
                return None, '只有故事生成失败的比赛可以重试'
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id, with_for_update=True)
            if not activity:
                return None, '活动不存在'
            try:
                S1TavernActivityService._freeze_gameplay_contract(activity)
            except RuntimeError as exc:
                return None, str(exc)
            if _shared_narrative_quota_is_full():
                return None, '今日主叙事共享配额已用尽'
            previous_ai_requested_at = match.ai_requested_at
            result_data['story_status'] = 'generating'
            match.result_data = result_data
            match.ai_requested_at = now
            match.generation_started_at = now
            match.generation_finished_at = None
            match.generation_attempts = int(match.generation_attempts or 0) + 1
            match.prompt_version = PROMPT_VERSION
            match.last_error = None
        if not S1TavernActivityService._submit_match_task(match_id, force_retry=True):
            S1TavernActivityService._mark_enqueue_failed(
                match_id,
                restore_ai_requested_at=previous_ai_requested_at,
            )
            return None, '重试任务入队失败'
        return {'queued': True, 'match_id': match_id}, None

    @staticmethod
    def _settle_match(match_id: int, *, now: datetime) -> bool:
        activity_id = None
        stage_number = None
        with db_manager.transaction():
            match = AdventurerS1ActivityMatch.query.session.get(AdventurerS1ActivityMatch, match_id, with_for_update=True)
            if not match or match.status != 'awaiting_reveal' or match.reveal_at > now:
                return False
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, match.activity_id, with_for_update=True)
            participants = (
                AdventurerS1ActivityParticipant.query
                .filter_by(match_id=match.id)
                .order_by(AdventurerS1ActivityParticipant.draw_slot)
                .with_for_update().all()
            )
            checked_in_ids = [
                int(participant.adventurer_id)
                for participant in participants
                if participant.status == 'checked_in'
            ]
            try:
                gameplay_contract = S1TavernActivityService._resolve_gameplay_contract(activity)
            except RuntimeError:
                match.status = 'failed'
                match.last_error = 'activity_gameplay_contract_invalid_before_settlement'
                return False
            result_data = dict(match.result_data or {})
            ranking = None
            if result_data.get('gameplay_contract_id') == gameplay_contract['contract_id']:
                if result_data.get('resolution_kind') == 'forfeit_walkover':
                    candidate_ranking = result_data.get('ranking')
                    if (
                        isinstance(candidate_ranking, list)
                        and len(candidate_ranking) == len(checked_in_ids)
                        and {
                            int(row.get('adventurer_id'))
                            for row in candidate_ranking
                            if isinstance(row, dict)
                        } == set(checked_in_ids)
                    ):
                        ranking = candidate_ranking
                elif result_data.get('resolution_kind') == 'holdem_state_machine':
                    mechanical_result = verify_holdem_tournament_result(
                        result_data.get('mechanical_result'),
                        [
                            {
                                'adventurer_id': int(participant.adventurer_id),
                                'seat': int(participant.draw_slot),
                            }
                            for participant in participants
                            if participant.status == 'checked_in'
                        ],
                    )
                    if mechanical_result is not None:
                        ranking = mechanical_result['ranking']
            if ranking is None:
                match.status = 'failed'
                match.last_error = 'activity_result_invalid_before_settlement'
                return False
            rank_by_adv = {
                int(entry['adventurer_id']): int(entry['rank'])
                for entry in ranking
            }
            active_count = len(rank_by_adv)
            advance_count = int((activity.config_snapshot or {}).get('advance_count', 2))
            registrations = {
                row.id: row for row in AdventurerS1ActivityRegistration.query.filter(
                    AdventurerS1ActivityRegistration.id.in_(
                        [p.registration_id for p in participants],
                    ),
                ).with_for_update().all()
            }
            adventurers = {
                row.id: row for row in AdventurerS1.query.filter(
                    AdventurerS1.id.in_([p.adventurer_id for p in participants]),
                ).with_for_update().all()
            }
            for participant in participants:
                registration = registrations.get(participant.registration_id)
                adv = adventurers.get(participant.adventurer_id)
                rank = rank_by_adv.get(participant.adventurer_id)
                if rank is None:
                    participant.rank = None
                    participant.status = 'forfeit'
                    if registration:
                        registration.status = 'forfeit'
                        registration.eliminated_at = now
                elif match.is_final:
                    participant.rank = rank
                    participant.status = 'finalist'
                    if registration:
                        registration.final_rank = rank
                        registration.status = (
                            'champion' if rank == 1 else
                            'runner_up' if rank == 2 else
                            'eliminated'
                        )
                        registration.eliminated_at = now if rank > 1 else None
                elif rank <= min(advance_count, active_count):
                    participant.rank = rank
                    participant.status = 'advanced'
                    participant.advanced = True
                    if registration:
                        registration.status = 'advanced'
                else:
                    participant.rank = rank
                    participant.status = 'eliminated'
                    if registration:
                        registration.status = 'eliminated'
                        registration.eliminated_at = now
                if adv and adv.status == 'activity':
                    AdventurerS1Service.enter_rest(adv, now=now)
            match.status = 'settled'
            match.settled_at = now
            activity_id = match.activity_id
            stage_number = match.stage_number
        S1TavernActivityService._finish_stage_if_ready(
            int(activity_id), int(stage_number), now=now,
        )
        return True

    @staticmethod
    def _finish_stage_if_ready(activity_id: int, stage_number: int, *, now: datetime) -> None:
        pending = AdventurerS1ActivityMatch.query.filter(
            AdventurerS1ActivityMatch.activity_id == activity_id,
            AdventurerS1ActivityMatch.stage_number == stage_number,
            AdventurerS1ActivityMatch.status.in_(_STAGE_PENDING_MATCH_STATUSES),
        ).count()
        if pending:
            return
        with db_manager.transaction():
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id, with_for_update=True)
            if not activity or activity.status != 'running' or activity.current_stage != stage_number:
                return
            stage_matches = AdventurerS1ActivityMatch.query.filter_by(
                activity_id=activity_id,
                stage_number=stage_number,
            ).order_by(AdventurerS1ActivityMatch.group_number).all()
            if any(match.is_final for match in stage_matches):
                activity.status = 'completed'
                activity.completed_at = now
                return
            advanced = (
                AdventurerS1ActivityRegistration.query
                .filter_by(activity_id=activity_id, status='advanced')
                .order_by(AdventurerS1ActivityRegistration.id).all()
            )
            if len(advanced) <= 1:
                if advanced:
                    advanced[0].status = 'champion'
                    advanced[0].final_rank = 1
                activity.status = 'completed'
                activity.completed_at = now
                return
            earliest_start = min(match.starts_at for match in stage_matches)
            checkin_minutes = int((activity.config_snapshot or {}).get('checkin_minutes', 60))
            next_start = earliest_start + timedelta(days=1)
            if next_start <= now + timedelta(minutes=checkin_minutes):
                # 若本阶段因配额或故障跨日才结算，仍把下一阶段放到再下一天，
                # 不在同一自然日内临时连打两轮。
                next_start = now + timedelta(days=1)
            S1TavernActivityService._create_stage_locked(
                activity,
                advanced,
                stage_number=stage_number + 1,
                first_group_start_at=next_start,
            )

    @staticmethod
    def get_current(
        user_id: int,
        *,
        admin: bool = False,
        stage_number: int | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict:
        activity = (
            AdventurerS1Activity.query
            .order_by(
                AdventurerS1Activity.created_at.desc(),
                AdventurerS1Activity.id.desc(),
            )
            .first()
        )
        if not activity:
            return {'activity': None}
        return S1TavernActivityService.get_activity_page(
            activity.id,
            user_id=user_id,
            admin=admin,
            stage_number=stage_number,
            page=page,
            per_page=per_page,
        )

    @staticmethod
    def get_activity_page(
        activity_id: int,
        *,
        user_id: int | None = None,
        admin: bool = False,
        stage_number: int | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict:
        """按阶段分页读取比赛；当前玩家的场次独立返回，不受翻页影响。"""
        activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id)
        if not activity:
            return {'activity': None}

        selected_stage = max(1, int(stage_number or activity.current_stage or 1))
        selected_page = max(1, int(page or 1))
        default_page_size = _int_config(
            'ADVENTURER_S1_ACTIVITY_MATCH_PAGE_SIZE', 6,
        )
        max_page_size = _int_config(
            'ADVENTURER_S1_ACTIVITY_MATCH_PAGE_SIZE_MAX', 12,
        )
        selected_page_size = min(
            max_page_size,
            max(1, int(per_page or default_page_size)),
        )

        registration_count = (
            AdventurerS1ActivityRegistration.query
            .filter_by(activity_id=activity.id)
            .count()
        )
        my_registration = None
        if user_id is not None:
            my_registration = (
                AdventurerS1ActivityRegistration.query
                .filter_by(activity_id=activity.id, owner_user_id=user_id)
                .order_by(AdventurerS1ActivityRegistration.id)
                .first()
            )

        stage_rows = (
            AdventurerS1ActivityMatch.query
            .with_entities(
                AdventurerS1ActivityMatch.stage_number,
                func.count(AdventurerS1ActivityMatch.id),
            )
            .filter_by(activity_id=activity.id)
            .group_by(AdventurerS1ActivityMatch.stage_number)
            .order_by(AdventurerS1ActivityMatch.stage_number.desc())
            .all()
        )
        available_stages = {int(stage) for stage, _ in stage_rows}
        if available_stages and selected_stage not in available_stages:
            current_stage = int(activity.current_stage or 0)
            selected_stage = (
                current_stage
                if current_stage in available_stages
                else max(available_stages)
            )
        match_query = (
            AdventurerS1ActivityMatch.query
            .filter_by(activity_id=activity.id, stage_number=selected_stage)
        )
        total_matches = match_query.count()
        total_pages = max(1, math.ceil(total_matches / selected_page_size))
        selected_page = min(selected_page, total_pages)
        matches = (
            match_query
            .order_by(AdventurerS1ActivityMatch.group_number)
            .offset((selected_page - 1) * selected_page_size)
            .limit(selected_page_size)
            .all()
        )

        participants = (
            AdventurerS1ActivityParticipant.query
            .filter(AdventurerS1ActivityParticipant.match_id.in_([m.id for m in matches]))
            .order_by(
                AdventurerS1ActivityParticipant.match_id,
                AdventurerS1ActivityParticipant.draw_slot,
            )
            .all()
        ) if matches else []
        participants_by_match: dict[int, list[AdventurerS1ActivityParticipant]] = {}
        for participant in participants:
            participants_by_match.setdefault(participant.match_id, []).append(participant)
        serialized_matches = [
            S1TavernActivityService._serialize_match(
                match,
                participants_by_match.get(match.id, []),
                user_id=user_id,
                admin=admin,
            )
            for match in matches
        ]

        my_current_match = next(
            (
                match
                for match in serialized_matches
                if match['stage_number'] == activity.current_stage
                and any(row['is_mine'] for row in match['participants'])
            ),
            None,
        )
        if my_current_match is None and user_id is not None:
            current_match = (
                AdventurerS1ActivityMatch.query
                .join(
                    AdventurerS1ActivityParticipant,
                    AdventurerS1ActivityParticipant.match_id
                    == AdventurerS1ActivityMatch.id,
                )
                .filter(
                    AdventurerS1ActivityMatch.activity_id == activity.id,
                    AdventurerS1ActivityMatch.stage_number == activity.current_stage,
                    AdventurerS1ActivityParticipant.owner_user_id == user_id,
                )
                .order_by(AdventurerS1ActivityMatch.group_number)
                .first()
            )
            if current_match:
                current_participants = (
                    AdventurerS1ActivityParticipant.query
                    .filter_by(match_id=current_match.id)
                    .order_by(AdventurerS1ActivityParticipant.draw_slot)
                    .all()
                )
                my_current_match = S1TavernActivityService._serialize_match(
                    current_match,
                    current_participants,
                    user_id=user_id,
                    admin=admin,
                )

        final_ranking = (
            AdventurerS1ActivityRegistration.query
            .filter(
                AdventurerS1ActivityRegistration.activity_id == activity.id,
                AdventurerS1ActivityRegistration.final_rank.isnot(None),
            )
            .order_by(AdventurerS1ActivityRegistration.final_rank)
            .all()
        )
        return {
            'activity': S1TavernActivityService._serialize_activity(activity, admin=admin),
            'registration_count': registration_count,
            'my_registration': (
                S1TavernActivityService._serialize_registration(my_registration)
                if my_registration else None
            ),
            'my_current_match': my_current_match,
            'stages': [
                {
                    'stage_number': int(stage),
                    'match_count': int(match_count),
                }
                for stage, match_count in stage_rows
            ],
            'matches': serialized_matches,
            'match_pagination': {
                'stage_number': selected_stage,
                'page': selected_page,
                'per_page': selected_page_size,
                'total': total_matches,
                'total_pages': total_pages,
            },
            'final_ranking': [
                S1TavernActivityService._serialize_registration(row)
                for row in final_ranking
            ],
        }

    @staticmethod
    def get_activity(
        activity_id: int,
        *,
        user_id: int | None = None,
        admin: bool = False,
    ) -> dict:
        activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id)
        if not activity:
            return {'activity': None}
        registrations = (
            AdventurerS1ActivityRegistration.query
            .filter_by(activity_id=activity.id)
            .order_by(AdventurerS1ActivityRegistration.registered_at).all()
        )
        matches = (
            AdventurerS1ActivityMatch.query
            .filter_by(activity_id=activity.id)
            .order_by(
                AdventurerS1ActivityMatch.stage_number,
                AdventurerS1ActivityMatch.group_number,
            ).all()
        )
        participants = (
            AdventurerS1ActivityParticipant.query
            .filter(AdventurerS1ActivityParticipant.match_id.in_([m.id for m in matches]))
            .order_by(
                AdventurerS1ActivityParticipant.match_id,
                AdventurerS1ActivityParticipant.draw_slot,
            ).all()
        ) if matches else []
        participants_by_match: dict[int, list[AdventurerS1ActivityParticipant]] = {}
        for participant in participants:
            participants_by_match.setdefault(participant.match_id, []).append(participant)
        my_registration = next(
            (r for r in registrations if user_id is not None and r.owner_user_id == user_id),
            None,
        )
        return {
            'activity': S1TavernActivityService._serialize_activity(activity, admin=admin),
            'registration_count': len(registrations),
            'my_registration': (
                S1TavernActivityService._serialize_registration(my_registration)
                if my_registration else None
            ),
            'matches': [
                S1TavernActivityService._serialize_match(
                    match,
                    participants_by_match.get(match.id, []),
                    user_id=user_id,
                    admin=admin,
                )
                for match in matches
            ],
            'final_ranking': [
                S1TavernActivityService._serialize_registration(row)
                for row in sorted(
                    (r for r in registrations if r.final_rank is not None),
                    key=lambda r: int(r.final_rank or 999999),
                )
            ],
        }

    @staticmethod
    def _serialize_activity(activity: AdventurerS1Activity, *, admin: bool) -> dict:
        config_snapshot = dict(activity.config_snapshot or {})
        public_rule_defaults = {
            'poker_starting_stack': 1000,
            'poker_small_blind': 10,
            'poker_big_blind': 20,
            'poker_blind_raise_every_hands': 6,
        }
        gameplay_contract = get_tavern_activity_gameplay_contract(
            config_snapshot.get('gameplay_contract_id'),
        )
        data = {
            'id': activity.id,
            'name': activity.name,
            'description': activity.description,
            'status': activity.status,
            'current_stage': activity.current_stage,
            'first_stage_start_at': to_utc_iso(activity.first_stage_start_at),
            'rules': {
                key: config_snapshot.get(key, public_rule_defaults.get(key))
                for key in (
                    'group_size', 'advance_count', 'checkin_minutes',
                    'stamina_cost', 'poker_starting_stack',
                    'poker_small_blind', 'poker_big_blind',
                    'poker_blind_raise_every_hands',
                )
            },
            'gameplay': {
                key: gameplay_contract[key]
                for key in (
                    'display_name', 'rules_summary', 'table_rules', 'round_cycle',
                    'ranking_dimensions', 'ranking_rule', 'asset_boundary',
                )
                if key in gameplay_contract
            } if gameplay_contract else None,
        }
        if admin:
            data['activity_type'] = activity.activity_type
            data['registration_open_at'] = to_utc_iso(activity.registration_open_at)
            data['registration_close_at'] = to_utc_iso(activity.registration_close_at)
            data['completed_at'] = to_utc_iso(activity.completed_at)
            data['rules'].update({
                key: config_snapshot.get(key)
                for key in ('stagger_minutes', 'reveal_minutes', 'stamina_min_to_enter')
            })
            data['policies'] = dict(S1TavernActivityService._FROZEN_POLICIES)
            data['gameplay'] = gameplay_contract
            data['config_snapshot'] = config_snapshot
            data['created_by_user_id'] = activity.created_by_user_id
        return data

    @staticmethod
    def _serialize_registration(row: AdventurerS1ActivityRegistration) -> dict:
        return {
            'id': row.id,
            'adventurer_id': row.adventurer_id,
            'adventurer_name': row.adventurer_name,
            'status': row.status,
            'final_rank': row.final_rank,
            'registered_at': to_utc_iso(row.registered_at),
        }

    @staticmethod
    def _serialize_match(
        match: AdventurerS1ActivityMatch,
        participants: list[AdventurerS1ActivityParticipant],
        *,
        user_id: int | None,
        admin: bool,
    ) -> dict:
        revealed = match.status == 'settled'
        data = {
            'id': match.id,
            'stage_number': match.stage_number,
            'group_number': match.group_number,
            'is_final': bool(match.is_final),
            'status': match.status,
            'checkin_open_at': to_utc_iso(match.checkin_open_at),
            'starts_at': to_utc_iso(match.starts_at),
            'reveal_at': to_utc_iso(match.reveal_at),
            'participants': [
                {
                    'adventurer_id': p.adventurer_id,
                    'adventurer_name': p.adventurer_name,
                    'draw_slot': p.draw_slot,
                    'status': (
                        p.status if revealed or admin else
                        ('checked_in' if p.checked_in_at is not None else 'assigned')
                    ),
                    'checked_in': p.checked_in_at is not None,
                    'forfeit_reason': p.forfeit_reason if revealed or admin else None,
                    'rank': p.rank if revealed or admin else None,
                    'advanced': bool(p.advanced) if revealed or admin else False,
                    'is_mine': user_id is not None and p.owner_user_id == user_id,
                }
                for p in participants
            ],
            'story': str(match.story or '') if revealed or admin else None,
            'story_status': (
                str((match.result_data or {}).get('story_status') or '')
                if revealed or admin else None
            ),
        }
        if admin:
            audit_runs = list(
                (match.result_data or {}).get('narrative_attempt_audit') or [],
            )
            data.update({
                'generation_attempts': match.generation_attempts,
                'last_error': match.last_error,
                'prompt_version': match.prompt_version,
                'review': dict(match.review_data or {}),
                'narrative_attempt_audit': [
                    {
                        'run_number': int(run.get('run_number') or 0),
                        'outcome': str(run.get('outcome') or ''),
                        'failure_code': str(run.get('failure_code') or ''),
                        'attempts': [
                            {
                                'stage': str(attempt.get('stage') or ''),
                                'attempt': int(attempt.get('attempt') or 0),
                                'reason_code': str(attempt.get('reason_code') or ''),
                            }
                            for attempt in (run.get('attempts') or [])
                            if isinstance(attempt, dict)
                        ],
                    }
                    for run in audit_runs
                    if isinstance(run, dict)
                ],
            })
        return data

    @staticmethod
    def cancel_activity(activity_id: int) -> tuple[dict | None, str | None]:
        now = _portable_utc_now()
        with db_manager.transaction():
            # 与签到、开赛、结算保持 match -> activity 的锁顺序，避免取消时反向等待。
            open_matches = (
                AdventurerS1ActivityMatch.query
                .filter(
                    AdventurerS1ActivityMatch.activity_id == activity_id,
                    AdventurerS1ActivityMatch.status.notin_(('settled', 'cancelled')),
                )
                .order_by(AdventurerS1ActivityMatch.id)
                .with_for_update().all()
            )
            activity = AdventurerS1Activity.query.session.get(AdventurerS1Activity, activity_id, with_for_update=True)
            if not activity or activity.status not in _ACTIVE_ACTIVITY_STATUSES:
                return None, '活动不存在或已经结束'
            # 抽签或阶段推进可能在本事务等待 activity 锁期间刚创建了新组；
            # 锁住活动后再补查一次，确保取消不会留下仍可被 scheduler 推进的孤儿组。
            known_match_ids = {match.id for match in open_matches}
            newly_created_query = AdventurerS1ActivityMatch.query.filter(
                AdventurerS1ActivityMatch.activity_id == activity_id,
                AdventurerS1ActivityMatch.status.notin_(('settled', 'cancelled')),
            )
            if known_match_ids:
                newly_created_query = newly_created_query.filter(
                    AdventurerS1ActivityMatch.id.notin_(known_match_ids),
                )
            open_matches.extend(
                newly_created_query
                .order_by(AdventurerS1ActivityMatch.id)
                .with_for_update().all()
            )
            match_ids = [match.id for match in open_matches]
            participants = (
                AdventurerS1ActivityParticipant.query
                .filter(
                    AdventurerS1ActivityParticipant.match_id.in_(match_ids),
                    AdventurerS1ActivityParticipant.status == 'checked_in',
                ).all()
            ) if match_ids else []
            adv_ids = [p.adventurer_id for p in participants]
            for adv in AdventurerS1.query.filter(AdventurerS1.id.in_(adv_ids)).all():
                if adv.status == 'activity':
                    AdventurerS1Service.enter_rest(adv, now=now)
            for match in open_matches:
                match.status = 'cancelled'
            activity.status = 'cancelled'
            activity.completed_at = now
        return {'cancelled': True, 'activity_id': activity_id}, None


def tick_tavern_activities_job() -> None:
    """scheduler 入口必须显式提交，异常时回滚并继续下轮。"""
    try:
        stats = S1TavernActivityService.tick()
        db_manager.commit()
        if any(stats.values()):
            current_app.logger.info('S1 酒馆活动 tick: %s', stats)
    except Exception:
        db_manager.rollback()
        current_app.logger.exception('S1 酒馆活动 tick 失败')
    finally:
        db_manager.remove()
