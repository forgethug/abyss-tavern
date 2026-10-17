"""S1 探险日志分类索引的有界启动/后台回填。"""

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    resolve_s1_special_event_theme,
)


def backfill_missing_special_event_themes(
    batch_size: int = 200,
    *,
    max_batches: int | None = None,
) -> dict:
    """补齐旧日志分类；每批独立提交并释放连接。"""
    effective_batch_size = max(1, int(batch_size))
    batch_limit = (
        None if max_batches is None else max(0, int(max_batches))
    )
    last_id = 0
    processed = 0
    special = 0
    completed_batches = 0

    while batch_limit is None or completed_batches < batch_limit:
        try:
            rows = (
                db_manager.query(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.full_log,
                )
                .filter(
                    AdventurerS1ExpeditionLog.id > last_id,
                    AdventurerS1ExpeditionLog.special_event_theme.is_(None),
                )
                .order_by(AdventurerS1ExpeditionLog.id.asc())
                .limit(effective_batch_size)
                .with_for_update(skip_locked=True)
                .all()
            )
            if not rows:
                return {'processed': processed, 'special': special}

            mappings = []
            batch_special = 0
            for log_id, full_log in rows:
                last_id = int(log_id)
                theme = resolve_s1_special_event_theme(
                    full_log if isinstance(full_log, dict) else {},
                )
                mappings.append({
                    'id': int(log_id),
                    'special_event_theme': theme or '',
                })
                batch_special += int(bool(theme))

            ok, error = db_manager.bulk_update_mappings(
                AdventurerS1ExpeditionLog,
                mappings,
            )
            if not ok:
                raise RuntimeError(error or '批量回填特殊事件分类失败')

            committed, commit_error = db_manager.commit()
            if not committed:
                raise RuntimeError(
                    commit_error or '提交特殊事件分类回填失败'
                )
            processed += len(mappings)
            special += batch_special
            completed_batches += 1
        except Exception:
            db_manager.rollback()
            raise
        finally:
            # 只把标量游标和计数带到下一批；不让启动/维护回填长期占用
            # Runtime 的小型 QueuePool，也不跨批持有行锁或事务。
            db_manager.remove()

    return {'processed': processed, 'special': special}


def _bounded_config_int(
    config_key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(current_app.config.get(config_key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def backfill_missing_special_event_themes_job() -> dict:
    """单次有界回填；启动与 scheduler 共用同一幂等入口。"""
    return backfill_missing_special_event_themes(
        batch_size=_bounded_config_int(
            'ADVENTURER_S1_LOG_CATEGORY_BACKFILL_BATCH_SIZE',
            200,
            minimum=1,
            maximum=1000,
        ),
        max_batches=_bounded_config_int(
            'ADVENTURER_S1_LOG_CATEGORY_BACKFILL_MAX_BATCHES_PER_RUN',
            1,
            minimum=1,
            maximum=20,
        ),
    )
