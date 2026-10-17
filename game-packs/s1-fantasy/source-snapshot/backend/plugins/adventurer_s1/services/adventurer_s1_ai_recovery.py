"""S1 异步 AI 任务 recovery：重启 / worker 崩溃后补投未完成任务。"""

import logging

from db_repository import db_manager


logger = logging.getLogger(__name__)


def requeue_stuck_s1_ai_tasks(*, startup_recovery: bool = False) -> dict:
    """补投 S1 持久化状态机中未完成的异步 AI 任务。"""
    from services.async_task_service import AsyncTaskService
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
        _requeue_pending_s1_expedition_ai,
    )
    from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import (
        requeue_stale_observer_reviews,
    )
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
        AdventurerS1DataAppealService,
    )
    from plugins.adventurer_s1.services.adventurer_s1_building_ai_recovery_service import (
        AdventurerS1BuildingAiRecoveryService,
    )

    # Producer Web 也会执行插件启动钩子，但它并不拥有 FIFO。只有真正的
    # consumer 才能把“启动”解释为上一 AI worker 已停止；否则会误抢仍在
    # 独立 worker 中执行的数据库租约。
    effective_startup_recovery = bool(
        startup_recovery
        and AsyncTaskService.can_run_database_startup_recovery()
    )

    component_errors: list[str] = []

    def _run_component(name: str, callback):
        try:
            return callback()
        except Exception:
            if effective_startup_recovery:
                raise
            component_errors.append(name)
            logger.exception('S1 AI recovery 组件失败 component=%s', name)
            return 0
        finally:
            # 每个恢复器都是独立工作单元；异常或成功后都不得把 ORM session
            # 与连接泄漏给下一个组件。
            db_manager.remove()

    stats = {
        'expedition': _run_component(
            'expedition',
            lambda: _requeue_pending_s1_expedition_ai(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'observer_reviews': _run_component(
            'observer_reviews',
            lambda: requeue_stale_observer_reviews(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'data_appeals': _run_component(
            'data_appeals',
            lambda: AdventurerS1DataAppealService.requeue_stuck_ai(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'tavern_relief': _run_component(
            'tavern_relief',
            lambda: AdventurerS1DataAppealService.requeue_stuck_tavern_relief(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'player_mimo_reviews': _run_component(
            'player_mimo_reviews',
            lambda: AdventurerS1DataAppealService.requeue_stuck_player_mimo_reviews(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'player_revert_requests': _run_component(
            'player_revert_requests',
            lambda: AdventurerS1DataAppealService.requeue_stuck_player_revert_requests(
                startup_recovery=effective_startup_recovery,
            ),
        ),
        'building_ai': _run_component(
            'building_ai',
            lambda: AdventurerS1BuildingAiRecoveryService.requeue_stuck_building_ai(
                startup_recovery=effective_startup_recovery,
            ),
        ),
    }
    if component_errors:
        raise RuntimeError(
            'S1 AI recovery component failure: '
            + ','.join(component_errors)
        )
    return stats
