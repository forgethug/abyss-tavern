"""S1 小队页面的 Redis 版本号，用于条件读取聚合面板。"""
import logging
import time
from typing import Optional

from flask import current_app

from utils.redis_pool import get_client


logger = logging.getLogger(__name__)

_VERSION_KEY_PREFIX = 'adventurer_s1:board_version'


def get_board_version(board: str) -> Optional[str]:
    """读取面板版本；Redis 不可用时返回 None，让调用方安全降级为完整查询。"""
    client = get_client(decode_responses=True)
    if client is None:
        return None
    try:
        value = client.get(f'{_VERSION_KEY_PREFIX}:{board}')
        bucket_seconds = max(
            1,
            int(current_app.config['ADVENTURER_S1_BOARD_VERSION_SAFETY_BUCKET_SECONDS']),
        )
        time_bucket = int(time.time() // bucket_seconds)
        return f'{value or "0"}:{time_bucket}'
    except Exception as exc:
        logger.warning('读取 S1 %s 面板版本失败，将返回完整数据: %s', board, exc)
        return None


def bump_board_version(board: str) -> None:
    """数据写入成功后原子递增面板版本；版本服务故障不得阻断业务写入。"""
    client = get_client(decode_responses=True)
    if client is None:
        return
    try:
        client.incr(f'{_VERSION_KEY_PREFIX}:{board}')
    except Exception as exc:
        logger.warning('递增 S1 %s 面板版本失败: %s', board, exc)
