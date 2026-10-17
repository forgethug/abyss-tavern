from abc import ABC, abstractmethod
from typing import List, Dict, Any, Type
from flask import Blueprint


class PluginBase(ABC):
    """所有玩法插件的基类。

    每个玩法（股票、钓鱼、赛马等）都实现此接口，
    由 PluginRegistry 统一管理和加载。
    """

    # 插件默认配置，加载时合并到 app.config
    default_config: dict = {}

    # ---- 必须实现 ----

    @property
    @abstractmethod
    def name(self) -> str:
        """插件唯一标识，如 'stock', 'fishing', 'race'"""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """插件显示名称，如 '股票交易', '钓鱼'"""
        pass

    @abstractmethod
    def get_blueprint(self) -> Blueprint:
        """返回此插件的 Flask Blueprint"""
        pass

    # ---- 可选实现 ----

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return ""

    @property
    def icon(self) -> str:
        """导航栏图标（emoji 或图标名）"""
        return ""

    @property
    def dependencies(self) -> List[str]:
        """依赖的其他插件名称列表。

        例如公司插件依赖股票插件：dependencies = ['stock']
        注册时会检查依赖是否已安装。
        """
        return []

    @property
    def is_critical(self) -> bool:
        """是否为核心插件。

        核心插件初始化失败时会中断应用启动；
        非核心插件初始化失败时降级运行，跳过该插件。
        """
        return False

    @property
    def nav_items(self) -> List[Dict[str, Any]]:
        """导航栏菜单项。

        格式: [
            {
                'label': '股票市场',
                'icon': '📈',
                'path': '/stock/market',
                'order': 10,  # 排序权重，越小越靠前
            }
        ]
        """
        return []

    def get_models(self) -> List[Type]:
        """返回此插件的 SQLAlchemy Model 类列表。

        PluginRegistry 会在注册时自动将这些 Model
        添加到 SQLAlchemy metadata，确保建表和外键正常工作。
        """
        return []

    def get_scheduler_tasks(self) -> List[Dict[str, Any]]:
        """返回此插件的定时任务配置列表。

        格式: [
            {
                'id': 'stock_fluctuate',
                'func': self._fluctuate,
                'trigger': 'interval',       # interval | cron | date
                'seconds': 60,               # interval 参数
                # 'hour': 9, 'minute': 30,   # cron 参数
                'kwargs': {},                # 传给 func 的额外参数
            }
        ]

        SchedulerService 会自动收集所有插件的任务并注册。
        """
        return []

    def get_currencies(self) -> List[Dict[str, Any]]:
        """返回此插件定义的货币列表。

        格式: [
            {
                'code': 'fish_coin',              # 货币编码，全局唯一
                'name': '鱼鱼币',                  # 显示名称
                'description': '钓鱼系统专用货币',    # 描述
                'icon': '🐟',                     # emoji 图标
                'sort_order': 10,                 # 排序序号
            }
        ]

        插件加载时，核心系统会自动将这些货币注册到 currencies 表。
        如果货币已存在（按 code 判断），则跳过不覆盖。
        插件卸载时不会删除货币记录。
        """
        return []

    def get_achievements(self) -> List[Dict[str, Any]]:
        """返回此插件定义的成就列表。

        格式: [
            {
                'code': 'race_50_wins',           # 成就编码，全局唯一
                'name': '赛马娘',                  # 成就名称
                'description': '赢得50场赛跑冠军',  # 成就描述
                'icon': '🏇',                     # emoji 图标
                'category': 'race',               # 分类
                'condition_type': 'race_wins',    # 条件类型
                'condition_value': 50,            # 条件值
                'reward': 50000,                  # 奖励金额
                'title_name': '赛马娘',           # 关联称号名（可选）
            }
        ]

        插件加载时，核心系统会自动将这些成就注册到 AchievementService。
        """
        return []

    def get_achievement_checkers(self) -> Dict[str, Any]:
        """返回此插件的成就进度查询函数映射。

        格式: {
            'race_wins': check_race_wins,
            'race_consecutive_top3': check_consecutive_top3,
        }

        检查函数签名: checker(user_id, user_obj=user) -> dict
        返回的 dict 会合并到进度字典中，key 应与 condition_type 对应。

        AchievementService 在查询用户进度时会自动调用这些函数。
        """
        return {}

    def get_achievement_snapshot_hooks(self):
        """返回成就快照钩子，用于合并重复 DB 查询。

        begin_fn(user_id) / end_fn() 在 AchievementService.check_all_for_user 内调用。
        可选第三项 batch_load_fn(user_ids) 为调度器批量预取并返回 {user_id: snapshot}；
        begin_fn 需兼容 begin_fn(user_id, snapshot)。批量预取失败时自动回退单用户路径。
        无快照需求时返回 None。
        """
        return None

    # ---- 生命周期钩子 ----

    def on_install(self, app) -> None:
        """插件安装时调用（首次启用）。可用于创建初始数据。"""
        pass

    def on_uninstall(self, app) -> None:
        """插件卸载时调用。可用于清理数据。"""
        pass

    def on_enable(self, app) -> None:
        """插件启用时调用。"""
        pass

    def on_disable(self, app) -> None:
        """插件禁用时调用。"""
        pass

    def on_user_bankruptcy(self, user_id: int, **kwargs) -> None:
        """用户破产时的清理钩子。

        每个插件应在此方法中清理该用户在本插件中的数据。
        BankruptcyService 会通过 HookManager 触发此钩子。

        kwargs 可包含:
            farewell_message: 用户破产感言
        """
        pass

    def on_season_reset(self, season_id: int) -> None:
        """赛季重置时的清理钩子。

        每个插件应在此方法中清理本插件的所有赛季数据。
        NewSeasonService 会通过 HookManager 触发此钩子。
        """
        pass

    def on_user_login(self, user_id: int) -> None:
        """用户登录钩子。"""
        pass

    def register_hooks(self, hook_manager) -> None:
        """注册自定义钩子。

        插件可以通过此方法注册自己的钩子，
        供其他插件或核心服务调用。

        示例:
            hook_manager.register('stock_delisted', self._on_stock_delisted, priority=10)
        """
        pass

    def register_events(self, event_bus) -> None:
        """注册事件监听器。

        插件可以通过此方法监听其他插件发出的事件，
        实现跨插件的松耦合通信。

        示例:
            event_bus.on('stock_delisting', self._handle_stock_delisting)
        """
        pass

    def init_plugin(self, app) -> None:
        """插件初始化（应用启动时调用）。

        可用于执行需要在应用上下文中完成的初始化操作，
        如加载配置、建立连接等。
        """
        pass

    def after_async_task_init(self, app) -> None:
        """异步任务队列与 worker 启动后的插件生命周期钩子。"""
        pass
