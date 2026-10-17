"""S1 任务栏动态冷门地图委托。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import logging
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func

from db_repository import db_manager


logger = logging.getLogger(__name__)

_SOURCE_TASK_ID = 'dynamic_map_bounty'
_TASK_ID_PREFIX = 'map_bounty_'


class S1DynamicMapBountyService:
    """按近期完成探险参与人次生成低热度地图任务批次。"""

    @staticmethod
    def _config_int(key: str, default: int, *, minimum: int = 1) -> int:
        try:
            return max(minimum, int(current_app.config.get(key, default)))
        except (TypeError, ValueError):
            return max(minimum, default)

    @staticmethod
    def _eligible_dungeons() -> list[dict]:
        from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_active_dungeons

        max_level = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_MAX_DUNGEON_LEVEL', 14,
        )
        return [
            dungeon for dungeon in get_s1_active_dungeons()
            if int(dungeon.get('max_level') or 999) <= max_level
        ]

    @staticmethod
    def _participant_visits(since: datetime) -> dict[str, int]:
        from plugins.adventurer_s1.models import (
            AdventurerS1ExpeditionLog,
            AdventurerS1ExpeditionLogMember,
        )

        rows = (
            db_manager.query(
                AdventurerS1ExpeditionLog.target_dungeon,
                func.count(AdventurerS1ExpeditionLogMember.id).label('participant_visits'),
            )
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
            )
            .filter(
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.created_at >= since,
            )
            .group_by(AdventurerS1ExpeditionLog.target_dungeon)
            .all()
        )
        return {str(row.target_dungeon): int(row.participant_visits or 0) for row in rows}

    @staticmethod
    def _recent_target_dungeons(now: datetime) -> set[str]:
        from plugins.adventurer_s1.models import AdventurerS1TaskOverride

        cooldown_hours = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_TARGET_COOLDOWN_HOURS', 4,
        )
        rows = (
            db_manager.query(AdventurerS1TaskOverride.overrides_json)
            .filter(
                AdventurerS1TaskOverride.source_template_id == _SOURCE_TASK_ID,
                AdventurerS1TaskOverride.created_at >= now - timedelta(hours=cooldown_hours),
            )
            .all()
        )
        return {
            str((row.overrides_json or {}).get('required_dungeon')).strip()
            for row in rows
            if str((row.overrides_json or {}).get('required_dungeon') or '').strip()
        }

    @staticmethod
    def _select_target(now: datetime) -> Tuple[Optional[dict], Optional[str]]:
        dungeons = S1DynamicMapBountyService._eligible_dungeons()
        if not dungeons:
            return None, '没有符合等级限制的普通副本'

        lookback_hours = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_LOOKBACK_HOURS', 24,
        )
        visits = S1DynamicMapBountyService._participant_visits(
            now - timedelta(hours=lookback_hours),
        )
        recent_targets = S1DynamicMapBountyService._recent_target_dungeons(now)
        cooled = [dungeon for dungeon in dungeons if dungeon['name'] not in recent_targets]
        candidates = cooled or dungeons
        candidates.sort(key=lambda dungeon: (visits.get(dungeon['name'], 0), dungeon['name']))

        pool_size = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_CANDIDATE_POOL_SIZE', 5,
        )
        pool = candidates[:pool_size]
        highest_count = max(visits.get(dungeon['name'], 0) for dungeon in pool)
        weights = [highest_count - visits.get(dungeon['name'], 0) + 1 for dungeon in pool]
        selected = random.choices(pool, weights=weights, k=1)[0]
        return {
            **selected,
            'participant_visits': visits.get(selected['name'], 0),
            'lookback_hours': lookback_hours,
        }, None

    @staticmethod
    def _task_id(now: datetime, dungeon_name: str) -> str:
        suffix = hashlib.sha1(dungeon_name.encode('utf-8')).hexdigest()[:6]
        return f'{_TASK_ID_PREFIX}{now:%Y%m%d%H%M%S}_{suffix}'

    @staticmethod
    def _task_overrides(target: dict) -> dict:
        batch_size = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_BATCH_SIZE', 10,
        )
        reward_gold = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_REWARD_GOLD', 100,
        )
        dungeon_name = target['name']
        return {
            'name': f'冷门地图支援·{dungeon_name}',
            'description': (
                f'最近 {target["lookback_hours"]} 小时该地图仅记录 '
                f'{target["participant_visits"]} 人次完成探险。前往 {dungeon_name} 成功通关，'
                f'领取 {reward_gold} 金币；全服共 {batch_size} 份。'
            ),
            'required_dungeon': dungeon_name,
            'repeatable': True,
            'max_completions': batch_size,
            'rewards': {'gold': reward_gold},
            'narrative': (
                f'冒险者响应酒馆对 {dungeon_name} 的支援委托，平安归来后领取了约定的金币。'
            ),
        }

    @staticmethod
    def _cleanup_history(now: datetime) -> int:
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance, AdventurerS1TaskOverride

        retention_days = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_HISTORY_RETENTION_DAYS', 7,
        )
        stale_rows = (
            db_manager.query(AdventurerS1TaskOverride)
            .filter(
                AdventurerS1TaskOverride.source_template_id == _SOURCE_TASK_ID,
                AdventurerS1TaskOverride.enabled.is_(False),
                AdventurerS1TaskOverride.created_at < now - timedelta(days=retention_days),
            )
            .all()
        )
        if not stale_rows:
            return 0

        stale_ids = [row.task_template_id for row in stale_rows]
        accepted_ids = {
            row.task_template_id for row in db_manager.query(
                AdventurerS1TaskInstance.task_template_id,
            ).filter(
                AdventurerS1TaskInstance.task_template_id.in_(stale_ids),
                AdventurerS1TaskInstance.status == 'accepted',
            ).distinct().all()
        }
        deleted = 0
        for row in stale_rows:
            if row.task_template_id in accepted_ids:
                continue
            ok, err = db_manager.delete_instance(row, flush=False)
            if not ok:
                raise RuntimeError(err or f'删除历史动态任务失败: {row.task_template_id}')
            deleted += 1
        return deleted

    @staticmethod
    def refresh(now: Optional[datetime] = None) -> Tuple[Optional[dict], Optional[str]]:
        """刷新动态任务；健康批次保留，耗尽或到期时发布新批次。"""
        from plugins.adventurer_s1.models import AdventurerS1TaskInstance, AdventurerS1TaskOverride
        from plugins.adventurer_s1.services.adventurer_s1_task_service import (
            AdventurerS1TaskService,
            _invalidate_overrides_cache,
        )

        if not bool(current_app.config.get('ADVENTURER_S1_MAP_BOUNTY_ENABLED', True)):
            return {'status': 'disabled'}, None

        now = now or _portable_utc_now()
        duration_minutes = S1DynamicMapBountyService._config_int(
            'ADVENTURER_S1_MAP_BOUNTY_DURATION_MINUTES', 120,
        )
        changed = False
        try:
            active_rows = (
                db_manager.query(AdventurerS1TaskOverride)
                .filter_by(source_template_id=_SOURCE_TASK_ID, enabled=True)
                .order_by(AdventurerS1TaskOverride.created_at.desc())
                .with_for_update()
                .all()
            )
            active_ids = [row.task_template_id for row in active_rows]
            occupied_by_id: dict[str, int] = {}
            if active_ids:
                occupied_by_id = {
                    row.task_template_id: int(row.occupied_count or 0)
                    for row in db_manager.query(
                        AdventurerS1TaskInstance.task_template_id,
                        func.count(AdventurerS1TaskInstance.id).label('occupied_count'),
                    ).filter(
                        AdventurerS1TaskInstance.task_template_id.in_(active_ids),
                        AdventurerS1TaskInstance.status.in_(('accepted', 'completed')),
                    ).group_by(AdventurerS1TaskInstance.task_template_id).all()
                }

            healthy = None
            for row in active_rows:
                payload = row.overrides_json or {}
                quota = max(1, int(payload.get('max_completions') or 1))
                occupied = occupied_by_id.get(row.task_template_id, 0)
                expired = row.created_at + timedelta(minutes=duration_minutes) <= now
                if healthy is None and occupied < quota and not expired:
                    healthy = row
                    continue
                row.enabled = False
                changed = True

            cleaned = S1DynamicMapBountyService._cleanup_history(now)
            changed = changed or cleaned > 0
            if healthy is not None:
                ok, err = db_manager.commit()
                if not ok:
                    return None, err
                if changed:
                    _invalidate_overrides_cache()
                return {
                    'status': 'kept',
                    'task_template_id': healthy.task_template_id,
                    'required_dungeon': (healthy.overrides_json or {}).get('required_dungeon'),
                    'occupied': occupied_by_id.get(healthy.task_template_id, 0),
                    'cleaned': cleaned,
                }, None

            target, target_err = S1DynamicMapBountyService._select_target(now)
            if target_err:
                db_manager.rollback()
                return None, target_err
            task_id = S1DynamicMapBountyService._task_id(now, target['name'])
            existing = db_manager.query(AdventurerS1TaskOverride).filter_by(
                task_template_id=task_id,
            ).first()
            if existing:
                existing.enabled = True
                existing.overrides_json = S1DynamicMapBountyService._task_overrides(target)
                existing.created_at = now
                existing.updated_at = now
            else:
                db_manager.add(AdventurerS1TaskOverride(
                    task_template_id=task_id,
                    source_template_id=_SOURCE_TASK_ID,
                    enabled=True,
                    overrides_json=S1DynamicMapBountyService._task_overrides(target),
                    created_at=now,
                    updated_at=now,
                ))
            ok, err = db_manager.commit()
            if not ok:
                return None, err
        except Exception as exc:
            db_manager.rollback()
            logger.exception('刷新 S1 动态地图委托失败')
            return None, str(exc)

        _invalidate_overrides_cache()
        AdventurerS1TaskService._emit_task_board_changed(
            reason='dynamic_map_bounty_refresh',
            task_id=task_id,
            task_name=f'冷门地图支援·{target["name"]}',
        )
        return {
            'status': 'created',
            'task_template_id': task_id,
            'required_dungeon': target['name'],
            'participant_visits': target['participant_visits'],
            'cleaned': cleaned,
        }, None


def refresh_dynamic_map_bounty_job() -> None:
    """Scheduler 入口；服务内显式提交，避免 session remove 回滚。"""
    result, err = S1DynamicMapBountyService.refresh()
    if err:
        logger.error('S1 动态地图委托定时刷新失败: %s', err)
    elif result and result.get('status') == 'created':
        logger.info('S1 动态地图委托已刷新: %s', result)
