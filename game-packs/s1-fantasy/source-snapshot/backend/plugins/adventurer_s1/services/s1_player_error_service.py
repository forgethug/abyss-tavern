"""玩家错误提示与服务端异常诊断的边界。"""
from __future__ import annotations

import logging

from db_repository.exceptions import ConcurrencyError, DatabaseError

logger = logging.getLogger(__name__)


class S1PlayerVisibleError(RuntimeError):
    """已由业务校验生成、允许告知玩家的原因；禁止包装原始技术异常文本。"""


def player_operation_failure(exc: Exception, operation: str = '操作失败') -> str:
    """显式业务原因可公开，其余异常仅记日志，不解析异常文本猜测安全性。"""
    from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
        CATALOG_BUSY_ERROR, CatalogBusyError,
    )
    from plugins.adventurer_s1.services.s1_realm_service import (
        S1RealmActiveSlotBusyError, S1RealmActiveSlotError,
    )

    current = exc
    seen = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, S1PlayerVisibleError):
            return f'{operation}：{current}'
        if isinstance(current, CatalogBusyError):
            return f'{operation}：{CATALOG_BUSY_ERROR}'
        # 槽位异常也可能携带远端响应正文，仅按明确类型给出业务提示。
        if isinstance(current, S1RealmActiveSlotBusyError):
            return f'{operation}：该账号正在生成另一个冒险者，请稍后重试'
        if isinstance(current, S1RealmActiveSlotError):
            return f'{operation}：每名玩家在一二区合计只能有 1 个存活冒险者'
        if isinstance(current, ConcurrencyError):
            logger.warning(
                'S1 %s: concurrency conflict', operation,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return f'{operation}，状态已变化，请刷新后重试'
        if isinstance(current, DatabaseError):
            current = current.original_error
        else:
            break
    logger.error('S1 %s', operation, exc_info=(type(exc), exc, exc.__traceback__))
    return f'{operation}，请稍后重试'


def player_database_result(result: tuple) -> tuple:
    """数据库门面已经返回错误文本时，在来源处阻止其进入玩家响应。"""
    value, detail = result
    if not detail:
        return value, detail
    logger.error('S1 database operation failed: %s', detail)
    return value, '保存失败，请稍后重试'
