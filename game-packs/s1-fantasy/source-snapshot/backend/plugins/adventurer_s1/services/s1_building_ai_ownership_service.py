"""S1 建筑异步 AI 的消费 Worker 启动所有权。"""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime

from utils.backend_boot import get_async_worker_boot_id


AI_WORKER_BOOT_ID_KEY = 'ai_worker_boot_id'
AI_WORKER_CLAIMED_AT_KEY = 'ai_worker_claimed_at'


def stamp_current_building_ai_owner(extra: dict | None) -> tuple[dict, bool]:
    """把建筑会话归属到当前 AI Worker；返回新快照与是否发生接管。"""
    owned = dict(extra) if isinstance(extra, dict) else {}
    current_boot_id = get_async_worker_boot_id()
    owner_changed = str(owned.get(AI_WORKER_BOOT_ID_KEY) or '') != current_boot_id
    if owner_changed:
        owned[AI_WORKER_BOOT_ID_KEY] = current_boot_id
        owned[AI_WORKER_CLAIMED_AT_KEY] = _portable_utc_now().isoformat()
    return owned, owner_changed


def building_ai_owner_is_current(extra: dict | None) -> bool:
    """旧数据没有 owner 时保持兼容；一旦认领，只允许同一 boot 落终态。"""
    stored = extra if isinstance(extra, dict) else {}
    owner_boot_id = str(stored.get(AI_WORKER_BOOT_ID_KEY) or '')
    return not owner_boot_id or owner_boot_id == get_async_worker_boot_id()
