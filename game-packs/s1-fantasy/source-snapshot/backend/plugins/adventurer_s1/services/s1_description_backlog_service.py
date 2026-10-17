"""Continue carried-asset work using the existing description AI and cache leases."""
import logging

from flask import current_app

from db_repository import db_manager
from db_repository.s1_description_backlog_repository import (
    forget_completed_description_sources,
    take_description_sources,
)

logger = logging.getLogger(__name__)


def submit_description_backlog(*, realm_code: str, max_items: int, max_batches: int) -> int:
    from plugins.adventurer_s1.services.s1_description_ai_service import (
        description_ai_summaries_enabled,
        enqueue_description_compression_items,
    )
    if max_batches <= 0 or not description_ai_summaries_enabled():
        return 0
    try:
        items = take_description_sources(
            realm_code, limit=max_items,
            revisit_seconds=max(1, int(current_app.config.get(
                'ADVENTURER_S1_DESCRIPTION_COMPRESS_ENQUEUE_RETRY_SECONDS', 60,
            ) or 60)),
        )
        if not items:
            return 0
        completed: set[str] = set()
        queued = enqueue_description_compression_items(
            items, max_batches=max_batches, max_items=max_items,
            completed_source_hashes=completed,
            asset_version_guard={
                'realm_code': realm_code,
                'source_hashes': [item['source_hash'] for item in items if item.get('asset_version_tracked')],
            },
        )
        # 仅清理由现有预约 owner 确认为完成且不需复查的来源。
        # 未入队、失败、处理中和冷却中的来源均保留，重启后仍能继续。
        forget_completed_description_sources(realm_code, completed)
        logger.info(
            'S1_DESCRIPTION_BACKLOG realm=%s visited=%s completed=%s queued=%s',
            realm_code, len(items), len(completed), queued,
        )
        return queued
    except Exception:
        db_manager.rollback()
        logger.exception('S1_DESCRIPTION_BACKLOG_FAILED realm=%s', realm_code)
        return 0
    finally:
        db_manager.remove()
