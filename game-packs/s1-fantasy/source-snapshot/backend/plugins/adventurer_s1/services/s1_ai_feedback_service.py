"""Lightweight player scoring for selected S1 AI outputs."""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta, timezone
from math import ceil

from db_repository import db_manager
from db_repository import s1_ai_feedback_repository as feedback_repo


_TARGET_CONFIG = {
    'appeal_first_review': {'label': '申诉一审'},
    'appeal_second_review': {'label': '申诉二审'},
    'expedition_log': {
        'label': '探险日志',
        'call_type': 'expedition_ai',
    },
    'item_assembly': {
        'label': '构装结果',
        'call_type': 'item_assembly_workshop_ai',
        'result_type': 'item_assembly_workshop',
    },
    'ember_kitchen': {
        'label': '余烬灶坊烹饪结果',
        'call_type': 'ember_kitchen_ai',
        'result_type': 'ember_kitchen',
    },
    'skill_training': {
        'label': '导师授艺',
        'call_type': 'skill_training_ai',
        'result_type': 'skill_training',
    },
}


def _appeal_target_specs(appeal) -> dict:
    """Only completed, player-visible replies can be rated by their owner."""
    if appeal.status not in ('approved', 'rejected', 'cancelled', 'reverted'):
        return {}
    if appeal.appeal_category == 'revert_request':
        return {}
    specs = {}
    if appeal.ai_status == 'done' and appeal._first_review_reason():
        call_type = {
            'rarity_review': 'rarity_review_ai',
            'tavern_relief': 'tavern_relief_ai',
        }.get(appeal.appeal_category, 'data_appeal_ai')
        specs[('appeal_first_review', int(appeal.id))] = ((call_type,), None)
    if appeal.review_ai_status in ('done', 'manual_ready') and appeal._second_review_reason():
        # Independent player review historically shares the regular review call type.
        # Its explicit request time bounds the original second-review receipts.
        before = None
        requests = appeal.player_review_requests or []
        if requests or appeal.player_review_status not in (None, '', 'none'):
            try:
                times = [datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
                         for row in requests]
                if not times:
                    return specs
                before = min(t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
                             for t in times).astimezone(timezone.utc).replace(tzinfo=None)
            except (KeyError, TypeError, ValueError, AttributeError):
                return specs
        specs[('appeal_second_review', int(appeal.id))] = (
            ('data_appeal_review_ai', 'data_appeal_review_retry_ai'), before,
        )
    return specs


def _player_target(
    target_type: str,
    target_id: int,
    feedback=None,
) -> dict:
    data = {
        'target_type': target_type,
        'target_id': int(target_id),
        'content_label': _TARGET_CONFIG[target_type]['label'],
        'quality_score': None,
        'preference_score': None,
    }
    if feedback is not None:
        player_feedback = feedback.to_player_dict()
        data['quality_score'] = player_feedback['quality_score']
        data['preference_score'] = player_feedback['preference_score']
    return data


def _parse_target_id(value) -> int | None:
    if type(value) is bool:
        return None
    try:
        target_id = int(value)
    except (TypeError, ValueError):
        return None
    return target_id if target_id > 0 else None


def _parse_score(value) -> int | None:
    if type(value) is not int:
        return None
    return value if 1 <= value <= 5 else None


def _building_target_spec(result: object) -> tuple[str, str, str] | None:
    if not isinstance(result, dict) or result.get('system_abort'):
        return None
    result_type = str(result.get('type') or '')
    for target_type in ('item_assembly', 'ember_kitchen', 'skill_training'):
        config = _TARGET_CONFIG[target_type]
        if result_type != config['result_type']:
            continue
        if target_type == 'skill_training' and result.get('success') is not True:
            return None
        usage_session_id = str(result.get('usage_session_id') or '').strip()
        if not usage_session_id:
            return None
        return target_type, config['call_type'], usage_session_id
    return None


def _resolve_target_and_usage(
    user_id: int,
    target_type: str,
    target_id: int,
):
    config = _TARGET_CONFIG.get(target_type)
    if config is None:
        return None, '不支持的 AI 评分类型'

    if target_type in ('appeal_first_review', 'appeal_second_review'):
        appeal = feedback_repo.get_owned_appeal(user_id, target_id)
        if appeal is None:
            return None, '当前内容不可评分'
        specs = _appeal_target_specs(appeal)
        key = (target_type, target_id)
        if key not in specs:
            return None, '当前内容不可评分'
        usage = feedback_repo.get_appeal_usage_map({key: specs[key]}).get(key)
    elif target_type == 'expedition_log':
        log = feedback_repo.get_expedition_log(target_id)
        if log is None or log.status != 'completed':
            return None, '当前内容不可评分'
        usage = feedback_repo.get_latest_success_token_usage(
            call_type=config['call_type'],
            expedition_log_id=target_id,
        )
    else:
        usage_log = feedback_repo.get_building_usage_log(target_id)
        if (
            usage_log is None
            or usage_log.adventurer is None
            or int(usage_log.adventurer.owner_id or 0) != int(user_id)
        ):
            return None, '当前内容不可评分'
        spec = _building_target_spec(usage_log.result)
        if spec is None or spec[0] != target_type:
            return None, '当前内容不可评分'
        usage = feedback_repo.get_latest_success_token_usage(
            call_type=config['call_type'],
            correlation_id=spec[2],
        )

    if usage is None:
        return None, '评分入口正在准备，请稍后再试'
    return usage, None


class S1AiFeedbackService:
    """Player-safe projections and full-admin feedback aggregation."""

    @staticmethod
    def get_appeal_targets(user_id: int, appeals: list) -> dict[int, dict]:
        specs = {}
        for appeal in appeals:
            if int(appeal.user_id) == int(user_id):
                specs.update(_appeal_target_specs(appeal))
        usages = feedback_repo.get_appeal_usage_map(specs)
        feedback_map = feedback_repo.get_feedback_map(user_id, usages)
        targets = {}
        for target_type, appeal_id in usages:
            stage = 'first_review' if target_type == 'appeal_first_review' else 'second_review'
            targets.setdefault(appeal_id, {})[stage] = _player_target(
                target_type, appeal_id, feedback_map.get((target_type, appeal_id)),
            )
        return targets

    @staticmethod
    def get_expedition_target(
        user_id: int,
        expedition_log_id: int,
    ) -> dict | None:
        usage, err = _resolve_target_and_usage(
            user_id,
            'expedition_log',
            expedition_log_id,
        )
        if err or usage is None:
            return None
        feedback = feedback_repo.get_feedback(
            user_id,
            'expedition_log',
            expedition_log_id,
        )
        return _player_target('expedition_log', expedition_log_id, feedback)

    @staticmethod
    def get_building_targets(
        user_id: int,
        usage_logs: list,
    ) -> dict[int, dict]:
        """Batch project feedback controls for a bounded activity page."""
        eligible: dict[int, tuple[str, str, str]] = {}
        for usage_log in usage_logs:
            spec = _building_target_spec(usage_log.result)
            if spec is not None:
                eligible[int(usage_log.id)] = spec
        if not eligible:
            return {}

        feedback_by_target = feedback_repo.get_feedback_map(
            user_id,
            ((spec[0], log_id) for log_id, spec in eligible.items()),
        )
        return {
            log_id: _player_target(
                spec[0],
                log_id,
                feedback_by_target.get((spec[0], log_id)),
            )
            for log_id, spec in eligible.items()
        }

    @staticmethod
    def build_new_building_target(result: object, usage_log_id: int | None) -> dict | None:
        """Build a player-only target for a freshly persisted socket result."""
        if not usage_log_id:
            return None
        spec = _building_target_spec(result)
        if spec is None:
            return None
        return _player_target(spec[0], int(usage_log_id))

    @staticmethod
    def submit(user_id: int, payload: object) -> tuple[dict | None, str | None]:
        if not isinstance(payload, dict):
            return None, '请求体必须是 JSON 对象'
        target_type = str(payload.get('target_type') or '').strip()
        target_id = _parse_target_id(payload.get('target_id'))
        quality_score = _parse_score(payload.get('quality_score'))
        preference_score = _parse_score(payload.get('preference_score'))
        if target_type not in _TARGET_CONFIG or target_id is None:
            return None, '评分目标无效'
        if quality_score is None or preference_score is None:
            return None, '两项评分都必须是 1 到 5 的整数'

        response = None
        with db_manager.transaction():
            if not feedback_repo.lock_feedback_author(user_id):
                return None, '用户不存在'
            usage, err = _resolve_target_and_usage(
                user_id,
                target_type,
                target_id,
            )
            if err or usage is None:
                return None, err or '当前内容不可评分'
            feedback = feedback_repo.upsert_feedback(
                user_id=user_id,
                target_type=target_type,
                target_id=target_id,
                quality_score=quality_score,
                preference_score=preference_score,
                token_usage=usage,
            )
            response = _player_target(target_type, target_id, feedback)

        if target_type in ('item_assembly', 'ember_kitchen', 'skill_training'):
            try:
                from plugins.adventurer_s1.services.adventurer_s1_service import (
                    invalidate_my_activity_cache,
                )
                invalidate_my_activity_cache(user_id)
            except Exception:
                pass
        return response, None

    @staticmethod
    def get_admin_dashboard(
        *,
        days: int | None,
        target_type: str | None,
        page: int,
        per_page: int,
    ) -> tuple[dict | None, str | None]:
        target_type = str(target_type or '').strip() or None
        if target_type in ('', 'all'):
            target_type = None
        if target_type is not None and target_type not in _TARGET_CONFIG:
            return None, '不支持的 AI 评分类型'
        if days is not None and (days < 1 or days > 3650):
            return None, '统计天数无效'

        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 20), 100))
        since = (
            _portable_utc_now() - timedelta(days=days)
            if days is not None and days > 0
            else None
        )
        total, unique_players, avg_quality, avg_preference = (
            feedback_repo.get_admin_summary(
                since=since,
                target_type=target_type,
            )
        )
        target_rows = feedback_repo.get_admin_target_breakdown(
            since=since,
            target_type=target_type,
        )
        route_rows = feedback_repo.get_admin_route_breakdown(
            since=since,
            target_type=target_type,
        )
        recent_rows, recent_total = feedback_repo.get_admin_recent_feedback(
            since=since,
            target_type=target_type,
            page=page,
            per_page=per_page,
        )

        from plugins.adventurer_s1.services.s1_token_usage_service import (
            get_call_type_label,
        )

        def _avg(value) -> float | None:
            return round(float(value), 2) if value is not None else None

        recent = []
        for feedback, user in recent_rows:
            item = feedback.to_admin_dict()
            item['content_label'] = _TARGET_CONFIG[feedback.target_type]['label']
            item['call_type_label'] = get_call_type_label(feedback.call_type)
            item['player_name'] = (
                (user.nickname or user.username or f'用户#{feedback.user_id}')
                if user is not None
                else f'用户#{feedback.user_id}'
            )
            recent.append(item)

        return {
            'window_days': days if days and days > 0 else None,
            'target_type': target_type,
            'target_options': [
                {'value': key, 'label': value['label']}
                for key, value in _TARGET_CONFIG.items()
            ],
            'summary': {
                'total_feedback': total,
                'unique_players': unique_players,
                'avg_quality': _avg(avg_quality),
                'avg_preference': _avg(avg_preference),
            },
            'by_target': [
                {
                    'target_type': row[0],
                    'content_label': _TARGET_CONFIG[row[0]]['label'],
                    'count': int(row[1] or 0),
                    'avg_quality': _avg(row[2]),
                    'avg_preference': _avg(row[3]),
                }
                for row in target_rows
            ],
            'by_route': [
                {
                    'call_type': row[0],
                    'call_type_label': get_call_type_label(row[0]),
                    'model': row[1],
                    'profile_id': row[2],
                    'provider': row[3],
                    'endpoint_host': row[4],
                    'api_key_slot': row[5],
                    'api_endpoint_slot': row[6],
                    'count': int(row[7] or 0),
                    'avg_quality': _avg(row[8]),
                    'avg_preference': _avg(row[9]),
                }
                for row in route_rows
            ],
            'recent': {
                'items': recent,
                'total': recent_total,
                'page': page,
                'per_page': per_page,
                'pages': max(1, ceil(recent_total / per_page)),
            },
        }, None
