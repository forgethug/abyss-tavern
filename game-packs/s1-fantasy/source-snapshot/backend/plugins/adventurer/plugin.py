from core.plugin_base import PluginBase
from flask import Blueprint
import os


# 模块级变量，用于 default_config 中跨键引用默认值
_DEEPSEEK_MODEL_DEFAULT = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')


class AdventurerPlugin(PluginBase):
    name = "adventurer"
    display_name = "冒险者"
    version = "1.0.0"
    icon = "⚔️"
    description = "深渊酒馆冒险者系统：AI探险、商店、组队、特质、装备、ICU基金等"
    dependencies = []

    default_config = {
        # DeepSeek AI 配置
        'DEEPSEEK_API_KEY': os.environ.get('DEEPSEEK_API_KEY', ''),
        'DEEPSEEK_API_BASE': os.environ.get('DEEPSEEK_API_BASE', ''),
        'DEEPSEEK_MODEL': _DEEPSEEK_MODEL_DEFAULT,
        # 深渊酒馆 AI 配置
        'ADVENTURER_PREPARE_AI_MODEL': os.environ.get('ADVENTURER_PREPARE_AI_MODEL', _DEEPSEEK_MODEL_DEFAULT),
        'ADVENTURER_PREPARE_AI_MAX_TOKENS': int(os.environ.get('ADVENTURER_PREPARE_AI_MAX_TOKENS', '200000')),
        'ADVENTURER_PREPARE_AI_MAX_CARRY_ITEMS': int(os.environ.get('ADVENTURER_PREPARE_AI_MAX_CARRY_ITEMS', '20')),
        'ADVENTURER_WAREHOUSE_CAPACITY': 100,
        'ADVENTURER_PREPARE_AI_COMPARE_ENABLED': os.environ.get('ADVENTURER_PREPARE_AI_COMPARE_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on'),
        'ADVENTURER_PREPARE_AI_COMPARE_MODEL': os.environ.get('ADVENTURER_PREPARE_AI_COMPARE_MODEL', 'deepseek-v4-flash'),
        'ADVENTURER_PREPARE_AI_COMPARE_MAX_TOKENS': int(os.environ.get('ADVENTURER_PREPARE_AI_COMPARE_MAX_TOKENS', '200000')),
        'ADVENTURER_PREPARE_AI_TIMEOUT_SECONDS': int(os.environ.get('ADVENTURER_PREPARE_AI_TIMEOUT_SECONDS', '600')),
        'ADVENTURER_EXPEDITION_AI_TIMEOUT_SECONDS': int(os.environ.get('ADVENTURER_EXPEDITION_AI_TIMEOUT_SECONDS', '600')),
        'ADVENTURER_AI_EXPEDITION_TASK_LOCK_TTL_SECONDS': int(os.environ.get('ADVENTURER_AI_EXPEDITION_TASK_LOCK_TTL_SECONDS', '1800')),
        'ADVENTURER_EXPEDITION_AI_MAX_RETRIES': int(os.environ.get('ADVENTURER_EXPEDITION_AI_MAX_RETRIES', '3')),
        'ADVENTURER_EXPEDITION_AI_VALIDATION_FEEDBACK_LIMIT': int(os.environ.get('ADVENTURER_EXPEDITION_AI_VALIDATION_FEEDBACK_LIMIT', '3')),
        'ADVENTURER_EXPEDITION_STRICT_CONSUMED_MATCH': os.environ.get('ADVENTURER_EXPEDITION_STRICT_CONSUMED_MATCH', 'false').lower() in ('1', 'true', 'yes', 'on'),
        'ADVENTURER_AUTO_RETRY_GENERATING_MINUTES': int(os.environ.get('ADVENTURER_AUTO_RETRY_GENERATING_MINUTES', '30')),
        'ADVENTURER_AUTO_RETRY_GENERATING_MAX_ATTEMPTS': int(os.environ.get('ADVENTURER_AUTO_RETRY_GENERATING_MAX_ATTEMPTS', '3')),
        'ADVENTURER_DANGER_HEALING_KEYWORDS': (
            '治疗药水', '高级治疗药水', '绷带', '急救', '圣水', '回复', '疗伤'
        ),
        'ADVENTURER_DANGER_RESCUE_KEYWORDS': (
            '护符', '怀表', '回溯', '圣杯', '庇护', '护心', '替身', '逆转', '复生', '护命'
        ),
        'ADVENTURER_DANGER_SCORE_RULES': {
            'underleveled_far': 2,
            'underleveled_slight': 1,
            'overleveled_far': -2,
            'overleveled_slight': -1,
            'no_healing_items': 2,
            'healing_items_insufficient': 1,
            'healing_items_abundant': -1,
            'no_rescue_items': 1,
            'rescue_items_abundant': -1,
            'trigger_special_event': 1,
            'extreme_crisis': 2,
            'special_event_abyss': 2,
            'special_event_stranger': 3,
            'special_event_unbeatable_horror': 4,
        },
        'ADVENTURER_DANGER_LEVEL_RULES': (
            {
                'min_score': 6,
                'level': '绝境',
                'desc': '明显越级，救援资源接近枯竭，任何失误都可能立刻演变成减员或整队崩盘。'
            },
            {
                'min_score': 4,
                'level': '极高',
                'desc': '对口资源偏少，战线随时可能断裂，若关键职责出错就很容易直接滑向濒死与撤离崩盘。'
            },
            {
                'min_score': 2,
                'level': '高',
                'desc': '局势不会轻松，补给与应对手段只能勉强支撑，若连续吃亏就会很快积累成重伤压力。'
            },
            {
                'max_score': -2,
                'level': '较低',
                'desc': '整体仍有明显余裕，更适合写成稳扎稳打、局部承压而非动辄濒死的推进。'
            },
            {
                'level': '中等',
                'desc': '有真实风险，但仍保留一定回旋空间；局势更适合随着资源消耗与判断对错逐步升温。'
            },
        ),
        'ADVENTURER_DANGER_UNBEATABLE_HORROR_SURVIVOR_SUFFIX': '若真有人活着回来，也只能是付出巨大代价后的极少数幸存者。',
        # 深渊酒馆其他配置
        'ADVENTURER_MAX_FORMING_PARTIES': 1,
        'ADVENTURER_LOG_MAX_PROGRESS_PUSHES': 8,
    }

    @property
    def nav_items(self):
        return [
            {'label': '深渊酒馆', 'icon': '⚔️', 'path': '/adventurer/list', 'order': 40},
        ]

    def get_blueprint(self):
        from plugins.adventurer.routes import adventurer_bp
        return adventurer_bp

    def get_models(self):
        from plugins.adventurer.models import (
            Adventurer, AdventurerParty, PartyMember, ExpeditionLog,
            AdventurerItem, AdventurerShopItem, AdventurerTrait,
            AdventurerFollow, AdventurerClaim, AdventurerChatMessage,
        )
        return [
            Adventurer, AdventurerParty, PartyMember, ExpeditionLog,
            AdventurerItem, AdventurerShopItem, AdventurerTrait,
            AdventurerFollow, AdventurerClaim, AdventurerChatMessage,
        ]

    def get_scheduler_tasks(self):
        from plugins.adventurer.services.adventurer_expedition_runner import (
            auto_game_loop,
            check_icu_recovery,
            check_normal_recovery,
            progress_expeditions,
            send_parties_to_expedition,
        )
        from plugins.adventurer.services.adventurer_party_service import form_parties
        from plugins.adventurer.services.adventurer_shop_service import (
            initialize_shop_items,
            restock_basic_consumables,
            ai_restock_items,
        )
        return [
            {
                'id': 'adventurer_auto_game_loop',
                'func': auto_game_loop,
                'trigger': 'interval',
                'seconds': 300,
            },
            {
                'id': 'adventurer_icu_recovery',
                'func': check_icu_recovery,
                'trigger': 'interval',
                'seconds': 60,
            },
            {
                'id': 'adventurer_normal_recovery',
                'func': check_normal_recovery,
                'trigger': 'interval',
                'seconds': 300,
            },
            {
                'id': 'adventurer_progress_expeditions',
                'func': progress_expeditions,
                'trigger': 'interval',
                'seconds': 300,
            },
            {
                'id': 'adventurer_send_parties',
                'func': send_parties_to_expedition,
                'trigger': 'interval',
                'seconds': 60,
            },
            {
                'id': 'adventurer_form_parties',
                'func': form_parties,
                'trigger': 'interval',
                'seconds': 120,
            },
        ]

    def on_user_bankruptcy(self, user_id, **kwargs):
        # 破产只影响股票相关玩法，不影响冒险者
        pass

    def on_season_reset(self, season_id):
        from plugins.adventurer.models import (
            Adventurer, AdventurerParty, PartyMember, ExpeditionLog,
            AdventurerItem, AdventurerShopItem, AdventurerTrait,
            AdventurerFollow, AdventurerClaim, AdventurerChatMessage,
        )
        for model in [PartyMember, ExpeditionLog, AdventurerFollow, AdventurerClaim,
                      AdventurerChatMessage, AdventurerShopItem, AdventurerItem,
                      AdventurerTrait, AdventurerParty]:
            model.query.delete()
        for adv in Adventurer.query.all():
            adv.status = 'idle'
            adv.hp = adv.max_hp
            adv.gold = 100.0
            adv.equipment = {}
            adv.traits = []
            adv.warehouse = []
            adv.icu_fund = 0.0
            adv.recovery_time = None
            adv.exp = 0
            adv.party_preference = 'auto'
            adv.rest_start_time = None
            adv.latest_score = None

    def register_events(self, event_bus):
        pass

    def register_hooks(self, hook_manager):
        pass

    def init_plugin(self, app):
        from plugins.adventurer.routes import admin_adventurer_bp
        from plugins.adventurer.services.adventurer_shop_service import (
            initialize_shop_items, clean_polluted_history_story,
        )
        app.register_blueprint(admin_adventurer_bp)
        with app.app_context():
            try:
                initialize_shop_items()
                clean_polluted_history_story()
            except Exception:
                pass
