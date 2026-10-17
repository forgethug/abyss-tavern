from runtime_core.time import utc_now as _portable_utc_now
import json
import math
import random
import re
from datetime import datetime
from flask import current_app
from db_repository.manager import db_manager
from plugins.adventurer.models import Adventurer, AdventurerParty
from models.system_config import SystemConfig

__all__ = [
    # 系统配置键
    'MAX_ADVENTURERS_KEY', 'DEFAULT_MAX_ADVENTURERS',
    'AUTO_PARTY_ENABLED_KEY', 'AUTO_PARTY_MANUAL_LIMIT_ENABLED_KEY',
    'AUTO_PARTY_MAX_FORMING_PARTIES_KEY', 'DEFAULT_AUTO_PARTY_MANUAL_LIMIT_ENABLED',
    'DEFAULT_AUTO_PARTY_MAX_FORMING_PARTIES',
    # 赛季末狂暴模式
    'BERSERK_MODE_ENABLED_KEY', 'BERSERK_EXP_MULTIPLIER', 'BERSERK_GOLD_MULTIPLIER',
    # 装备损伤阶段
    'EQUIPMENT_DAMAGE_STAGE_INTACT', 'EQUIPMENT_DAMAGE_STAGE_CRACKED',
    'EQUIPMENT_DAMAGE_STAGE_CRITICAL', 'VALID_EQUIPMENT_DAMAGE_STAGES',
    'VALID_EQUIPMENT_RARITIES',
    # 职业 / 性格 / 初始技能
    'CLASSES', 'PERSONALITIES', 'DEFAULT_SKILLS',
    # 副本列表
    'DUNGEONS', 'DUNGEON_UNIQUE_TIERS', 'DUNGEON_RANK_MAP',
    # 冒险者姓名池
    'MALE_NAMES', 'FEMALE_NAMES', 'LAST_NAMES',
    # 商店物品基础数据
    'BASIC_CONSUMABLES', 'INITIAL_EQUIPMENT_SEED',
    'RARITY_STOCK_LIMITS', 'RARITY_AI_RESTOCK_THRESHOLD', 'RARITY_PRICE_RANGES',
    # SystemConfig 辅助函数
    '_get_system_config_record', '_get_system_config_int', '_get_system_config_bool',
    '_set_system_config_value',
    'get_max_adventurers', 'set_max_adventurers',
    'get_auto_party_settings', 'set_auto_party_settings',
    '_get_dynamic_max_forming_parties',
    'is_berserk_mode_enabled', 'set_berserk_mode_enabled',
    'compute_special_event_probability',
    # 副本 / 组队工具函数
    '_get_adventurer_real_rank', '_pick_weighted_dungeon_for_adventurer',
    '_find_same_rank_forming_party',
    'get_adventurer_exp_required', 'get_dungeon_level_range',
    '_normalize_adventurer_score', 'calculate_expedition_exp_gain',
    # 商店定价
    '_random_price_by_rarity',
    # 通用数值安全转换
    '_coerce_int', '_coerce_float', '_coerce_config_bool',
    '_get_adventurer_ai_request_timeout',
    # 文本清理工具
    '_clean_compact_text', '_clean_persisted_text', '_to_clean_string_list',
    # AI 响应解析工具
    '_strip_markdown_code_fence', '_safe_json_loads',
    '_extract_json_object_from_text', '_build_ai_output_preview',
    '_extract_ai_message_text', '_build_ai_response_summary',
]


# ── 系统配置键 ──────────────────────────────────────────────────────────────
MAX_ADVENTURERS_KEY = 'max_adventurers'
DEFAULT_MAX_ADVENTURERS = 48
AUTO_PARTY_ENABLED_KEY = 'adventurer_auto_party'
AUTO_PARTY_MANUAL_LIMIT_ENABLED_KEY = 'adventurer_auto_party_manual_limit_enabled'
AUTO_PARTY_MAX_FORMING_PARTIES_KEY = 'adventurer_auto_party_max_forming_parties'
DEFAULT_AUTO_PARTY_MANUAL_LIMIT_ENABLED = True
DEFAULT_AUTO_PARTY_MAX_FORMING_PARTIES = 1

# ── 赛季末狂暴模式 ────────────────────────────────────────────────────────────
BERSERK_MODE_ENABLED_KEY = 'adventurer_berserk_mode'
BERSERK_EXP_MULTIPLIER = 5.0
BERSERK_GOLD_MULTIPLIER = 2.0

# ── 装备损伤阶段 ──────────────────────────────────────────────────────────────
EQUIPMENT_DAMAGE_STAGE_INTACT = '完好'
EQUIPMENT_DAMAGE_STAGE_CRACKED = '开裂'
EQUIPMENT_DAMAGE_STAGE_CRITICAL = '濒毁'
VALID_EQUIPMENT_DAMAGE_STAGES = {
    EQUIPMENT_DAMAGE_STAGE_INTACT,
    EQUIPMENT_DAMAGE_STAGE_CRACKED,
    EQUIPMENT_DAMAGE_STAGE_CRITICAL,
}
VALID_EQUIPMENT_RARITIES = {
    'common',
    'rare',
    'epic',
    'legendary',
}

# ── 职业 / 性格 / 初始技能 ────────────────────────────────────────────────────
CLASSES = ['战士', '法师', '牧师', '盗贼', '猎人', '骑士']
PERSONALITIES = ['勇敢', '谨慎', '贪婪', '鲁莽', '稳重', '狡猾', '傲慢', '怯懦', '仁慈', '残忍', '孤僻', '乐观']
DEFAULT_SKILLS = {
    '战士': [
        {"name": "顺劈斩", "desc": ""},
        {"name": "盾击", "desc": ""}
    ],
    '法师': [
        {"name": "火球术", "desc": ""},
        {"name": "冰霜护甲", "desc": ""}
    ],
    '牧师': [
        {"name": "次级治疗术", "desc": ""},
        {"name": "惩击", "desc": ""}
    ],
    '盗贼': [
        {"name": "背刺", "desc": ""},
        {"name": "潜行", "desc": ""}
    ],
    '猎人': [
        {"name": "瞄准射击", "desc": ""},
        {"name": "陷阱布置", "desc": ""}
    ],
    '骑士': [
        {"name": "冲锋", "desc": ""},
        {"name": "圣光护盾", "desc": ""}
    ]
}

# ── 副本列表 ──────────────────────────────────────────────────────────────────
DUNGEONS = [
    # Level 1-3
    {'name': '幽暗密林', 'min_level': 1, 'max_level': 3},
    {'name': '哥布林矿坑', 'min_level': 1, 'max_level': 3},
    {'name': '迷雾沼泽', 'min_level': 1, 'max_level': 3},

    # Level 2-5
    {'name': '叹息地牢', 'min_level': 2, 'max_level': 5},
    {'name': '废弃修道院', 'min_level': 2, 'max_level': 5},
    {'name': '剧毒钟乳洞', 'min_level': 2, 'max_level': 5},

    # Level 4-7
    {'name': '熔岩火山', 'min_level': 4, 'max_level': 7},
    {'name': '风暴尖塔', 'min_level': 4, 'max_level': 7},
    {'name': '狂沙荒漠', 'min_level': 4, 'max_level': 7},

    # Level 6-9
    {'name': '冰霜峡谷', 'min_level': 6, 'max_level': 9},
    {'name': '哀嚎深渊', 'min_level': 6, 'max_level': 9},
    {'name': '暗影迷宫', 'min_level': 6, 'max_level': 9},

    # Level 8-11
    {'name': '遗忘废墟', 'min_level': 8, 'max_level': 11},
    {'name': '陨落星都', 'min_level': 8, 'max_level': 11},
    {'name': '混沌王座', 'min_level': 8, 'max_level': 11},

    # Level 10-14
    {'name': '巨龙巢穴', 'min_level': 10, 'max_level': 14},
    {'name': '恶魔岛', 'min_level': 10, 'max_level': 14},
    {'name': '幽冥王都', 'min_level': 10, 'max_level': 14},

    # Level 15-19
    {'name': '诸神黄昏', 'min_level': 15, 'max_level': 19},
    {'name': '时空裂缝', 'min_level': 15, 'max_level': 19},
    {'name': '虚空神殿', 'min_level': 15, 'max_level': 19},

    # Level 20+
    {'name': '宇宙尽头', 'min_level': 20, 'max_level': 999},
    {'name': '混沌起源', 'min_level': 20, 'max_level': 999},
    {'name': '创世神塔', 'min_level': 20, 'max_level': 999}
]

DUNGEON_UNIQUE_TIERS = sorted({d['min_level'] for d in DUNGEONS})
DUNGEON_RANK_MAP = {d['name']: DUNGEON_UNIQUE_TIERS.index(d['min_level']) for d in DUNGEONS}

# ── 冒险者姓名池 ───────────────────────────────────────────────────────────────
MALE_NAMES = ["亚瑟", "林克", "罗兰", "卡尔", "雷克", "奥兹", "加雷斯", "里昂", "德莱厄斯", "格雷", "伊泽", "克雷格", "巴林", "霍克"]
FEMALE_NAMES = ["艾露", "菲奥娜", "塞琳", "米娅", "伊莉雅", "莉莉丝", "安娜", "贞德", "克莱尔", "西尔维娅", "芙蕾雅", "黛安娜", "艾莉丝"]
LAST_NAMES = ["·风暴", "·银刃", "·火歌", "·影步", "·铁石", "·星辰", "·光语", "·血斧", "·夜歌", "·月守", "·破晓", "·霜狼"]

# ── 商店物品基础数据 ──────────────────────────────────────────────────────────
BASIC_CONSUMABLES = [
    {"name": "治疗药水", "type": "consumable", "description": "恢复少量生命值", "rarity": "common", "price": 25000, "stock": 50},
    {"name": "高级治疗药水", "type": "consumable", "description": "恢复大量生命值", "rarity": "rare", "price": 80000, "stock": 20},
    {"name": "解毒草", "type": "consumable", "description": "解除中毒状态并恢复微量生命", "rarity": "common", "price": 15000, "stock": 50},
    {"name": "绷带", "type": "consumable", "description": "缓慢恢复少量生命值", "rarity": "common", "price": 5000, "stock": 50},
    {"name": "魔法泉水", "type": "consumable", "description": "恢复精神和法力", "rarity": "rare", "price": 50000, "stock": 20},
]

INITIAL_EQUIPMENT_SEED = [
    # Common
    {"name": "生锈的铁剑", "type": "weapon", "description": "略微提升物理伤害", "rarity": "common", "price": 30000, "stock": 10},
    {"name": "粗糙的皮甲", "type": "armor", "description": "提供基础的物理防护", "rarity": "common", "price": 35000, "stock": 10},
    {"name": "木制圆盾", "type": "armor", "description": "格挡部分轻微伤害", "rarity": "common", "price": 25000, "stock": 10},
    {"name": "矿工镐", "type": "weapon", "description": "可以用来挖掘或敲击硬物", "rarity": "common", "price": 20000, "stock": 15},

    # Rare
    {"name": "精钢长剑", "type": "weapon", "description": "锋利的剑刃，能轻易斩断普通护甲", "rarity": "rare", "price": 150000, "stock": 5},
    {"name": "秘银锁子甲", "type": "armor", "description": "轻便且坚固，有效抵御穿刺伤害", "rarity": "rare", "price": 200000, "stock": 5},
    {"name": "淬毒匕首", "type": "weapon", "description": "攻击时有概率使目标中毒，但容易误伤自己", "rarity": "rare", "price": 180000, "stock": 5},
    {"name": "学徒法杖", "type": "weapon", "description": "增强初级法术的威力", "rarity": "rare", "price": 160000, "stock": 5},

    # Epic
    {"name": "炎魔之手", "type": "weapon", "description": "大幅提升物理伤害，但战斗中会持续灼烧自身，导致额外失去生命值", "rarity": "epic", "price": 1200000, "stock": 2},
    {"name": "深渊长袍", "type": "armor", "description": "极大减免魔法伤害，但会扭曲心智，使用后永久降低智力", "rarity": "epic", "price": 1500000, "stock": 2},
    {"name": "雷霆战锤", "type": "weapon", "description": "攻击有几率召唤闪电链，极其沉重，使用后永久降低敏捷", "rarity": "epic", "price": 1800000, "stock": 2},
    {"name": "龙血药剂", "type": "consumable", "description": "饮用后永久极大幅度提升力量，但有极大概率导致躯体变异直接死亡", "rarity": "epic", "price": 1000000, "stock": 2},
    {"name": "光明使者之剑", "type": "weapon", "description": "极大幅度提升物理伤害，蕴含纯粹的神圣力量", "rarity": "epic", "price": 4000000, "stock": 2},
    {"name": "精灵王之铠", "type": "armor", "description": "极大幅度减免受到的伤害，轻如鸿毛", "rarity": "epic", "price": 4500000, "stock": 2},

    # Legendary
    {"name": "星界割裂者", "type": "weapon", "description": "无视一切防御的极品神器，但反噬极强，每次高强度战斗都会永久降低力量和敏捷", "rarity": "legendary", "price": 8000000, "stock": 1},
    {"name": "永恒守护胸甲", "type": "armor", "description": "免疫绝大多数物理伤害，但极其笨重，穿戴后会在战斗中处于被动挨打状态，导致失去生命值", "rarity": "legendary", "price": 10000000, "stock": 1},
    {"name": "诸神黄昏之弓", "type": "weapon", "description": "射出必中的毁灭光箭，但会引来神明诅咒，永久大幅降低幸运", "rarity": "legendary", "price": 12000000, "stock": 1},
    {"name": "时空回溯沙漏", "type": "consumable", "description": "濒死时自动触发，免除死亡并恢复部分生命，代价是永久失去大量力量、敏捷、智力和幸运", "rarity": "legendary", "price": 15000000, "stock": 1},
    {"name": "创世神之刃", "type": "weapon", "description": "能轻易斩断世间一切，释放毁天灭地的力量", "rarity": "legendary", "price": 35000000, "stock": 1},
    {"name": "奇迹之泪", "type": "consumable", "description": "服用后永久极大幅度全方位提升所有属性", "rarity": "legendary", "price": 40000000, "stock": 1},
]

RARITY_STOCK_LIMITS = {
    "common": 6,
    "rare": 10,
    "epic": 5,
    "legendary": 3,
    "useless": 3
}

RARITY_AI_RESTOCK_THRESHOLD = {
    "common": 3,
    "rare": 4,
    "epic": 1,
    "legendary": 0,
    "useless": 0
}

# AI 进货定价区间（单位：金币）。useless 是华而不实的小垃圾。
RARITY_PRICE_RANGES = {
    "common": (30000, 100000),
    "rare": (100000, 500000),
    "epic": (3000000, 10000000),
    "legendary": (10000000, 40000000),
    "useless": (500000, 5000000),
}


# ── SystemConfig 辅助函数 ─────────────────────────────────────────────────────

def _get_system_config_record(config_key: str) -> SystemConfig | None:
    return db_manager.system_config.find_one(config_key=config_key)


def _get_system_config_int(config_key: str, default: int, min_value: int = 1) -> int:
    config = _get_system_config_record(config_key)
    if config and config.config_value not in (None, ''):
        try:
            return max(min_value, int(config.config_value))
        except (TypeError, ValueError):
            pass
    return max(min_value, int(default))


def _get_system_config_bool(config_key: str, default: bool) -> bool:
    config = _get_system_config_record(config_key)
    if not config or config.config_value in (None, ''):
        return default
    return str(config.config_value).strip().lower() not in {'0', 'false', 'no', 'off'}


def _set_system_config_value(config_key: str, config_value: str, description: str) -> str | None:
    config = _get_system_config_record(config_key)
    if not config:
        config = SystemConfig(
            config_key=config_key,
            config_value=config_value,
            description=description
        )
        ok, err = db_manager.add(config)
        return err if not ok else None

    config.config_value = config_value
    config.updated_at = _portable_utc_now()
    return None


def get_max_adventurers() -> int:
    """获取最大冒险者数量限制，数据库没有则返回默认值48"""
    return _get_system_config_int(MAX_ADVENTURERS_KEY, DEFAULT_MAX_ADVENTURERS)


def set_max_adventurers(value: int) -> tuple[dict, str | None]:
    """设置最大冒险者数量限制"""
    try:
        value = max(1, int(value))
        write_err = _set_system_config_value(MAX_ADVENTURERS_KEY, str(value), '最大存活冒险者数量上限')
        if write_err:
            return None, write_err
        ok, err = db_manager.commit()
        if not ok:
            return None, err
        return {'max_adventurers': value}, None
    except Exception as e:
        return None, str(e)


def get_auto_party_settings() -> dict:
    """获取自动建队限制设置。"""
    manual_limit_enabled = _get_system_config_bool(
        AUTO_PARTY_MANUAL_LIMIT_ENABLED_KEY,
        DEFAULT_AUTO_PARTY_MANUAL_LIMIT_ENABLED
    )
    max_forming_parties = _get_system_config_int(
        AUTO_PARTY_MAX_FORMING_PARTIES_KEY,
        DEFAULT_AUTO_PARTY_MAX_FORMING_PARTIES
    )
    auto_party_enabled = _get_system_config_bool(AUTO_PARTY_ENABLED_KEY, True)
    return {
        'auto_party_enabled': auto_party_enabled,
        'manual_limit_enabled': manual_limit_enabled,
        'max_forming_parties': max_forming_parties
    }


def set_auto_party_settings(
    manual_limit_enabled: bool,
    max_forming_parties: int
) -> tuple[dict | None, str | None]:
    """设置自动建队的显式上限与模式开关。"""
    try:
        normalized_max = max(1, int(max_forming_parties))
        write_err = _set_system_config_value(
            AUTO_PARTY_MANUAL_LIMIT_ENABLED_KEY,
            'true' if manual_limit_enabled else 'false',
            '是否启用自动建队的手动最大同时组队数量限制'
        )
        if write_err:
            return None, write_err
        write_err = _set_system_config_value(
            AUTO_PARTY_MAX_FORMING_PARTIES_KEY,
            str(normalized_max),
            '自动建队允许同时存在的最大集结中小队数量'
        )
        if write_err:
            return None, write_err
        ok, err = db_manager.commit()
        if not ok:
            return None, err
        return {
            'manual_limit_enabled': manual_limit_enabled,
            'max_forming_parties': normalized_max
        }, None
    except Exception as e:
        return None, str(e)


def _get_dynamic_max_forming_parties(idle_count: int) -> int:
    """原始动态建队逻辑：空闲人数 / 10，至少允许 1 个。"""
    return max(1, idle_count // 10)


def is_berserk_mode_enabled() -> bool:
    """读取赛季末狂暴模式开关。默认关闭，仅在 SystemConfig 显式 true 时启用。"""
    return _get_system_config_bool(BERSERK_MODE_ENABLED_KEY, False)


def set_berserk_mode_enabled(enabled: bool) -> tuple[dict | None, str | None]:
    """设置赛季末狂暴模式开关。"""
    try:
        write_err = _set_system_config_value(
            BERSERK_MODE_ENABLED_KEY,
            'true' if enabled else 'false',
            '深渊酒馆赛季末狂暴模式：开启后经验x5、金币x2、特殊事件概率大幅提升、AI 偏向更高风险与更高掉率'
        )
        if write_err:
            return None, write_err
        ok, err = db_manager.commit()
        if not ok:
            return None, err
        return {'enabled': bool(enabled)}, None
    except Exception as e:
        return None, str(e)


def compute_special_event_probability(dungeon_min_level: int, berserk_mode: bool = False) -> float:
    """根据副本等级与狂暴模式开关计算特殊事件触发概率。

    常规：min(level * 0.03, 0.25)
    狂暴：min(0.55 + level * 0.05, 1.0)，即 Lv1=60%、Lv2=65%、…、Lv9+=100%
    """
    level = max(1, int(dungeon_min_level))
    if berserk_mode:
        return min(0.55 + level * 0.05, 1.0)
    return min(level * 0.03, 0.25)


# ── 副本 / 组队工具函数 ────────────────────────────────────────────────────────

def _get_adventurer_real_rank(adventurer: Adventurer) -> int:
    """计算冒险者当前最适合的副本阶级锚点。"""
    natural_ranks = [
        DUNGEON_UNIQUE_TIERS.index(d['min_level'])
        for d in DUNGEONS
        if d['min_level'] <= adventurer.level <= d['max_level']
    ]
    return max(natural_ranks) if natural_ranks else 0


def _pick_weighted_dungeon_for_adventurer(adventurer: Adventurer, adv_real_rank: int) -> str:
    """根据冒险者等级倾向挑选适合带队创建的新地牢。"""
    weights = []
    for dungeon in DUNGEONS:
        if adventurer.level < dungeon['min_level']:
            continue

        target_rank = DUNGEON_RANK_MAP.get(dungeon['name'], 0)
        rank_diff = abs(target_rank - adv_real_rank)
        if rank_diff > 1:
            continue

        weight = 100 if rank_diff == 0 else 10
        weights.append((dungeon['name'], weight))

    if not weights:
        return DUNGEONS[0]['name']

    total_weight = sum(weight for _, weight in weights)
    roll = random.uniform(0, total_weight)
    cumulative = 0
    for dungeon_name, weight in weights:
        cumulative += weight
        if roll <= cumulative:
            return dungeon_name
    return weights[-1][0]


def _find_same_rank_forming_party(
    forming_parties: list[AdventurerParty],
    party_member_counts: dict[int, int],
    target_rank: int
) -> AdventurerParty | None:
    """在同副本阶级且未满员的小队中挑一个并入。"""
    same_rank_parties = [
        party
        for party in forming_parties
        if party_member_counts.get(party.id, 0) < party.target_size
        and DUNGEON_RANK_MAP.get(party.target_dungeon, 0) == target_rank
    ]
    if not same_rank_parties:
        return None
    return random.choice(same_rank_parties)


def get_adventurer_exp_required(level: int) -> int:
    """计算冒险者当前等级升下一级所需经验。"""
    if level >= 10:
        extra_penalty = (level - 9) * 1000
        return level * 100 + extra_penalty
    return level * 100


def get_dungeon_level_range(target_dungeon: str | None) -> tuple[int, int]:
    """根据地牢名获取推荐等级区间。"""
    for dungeon_info in DUNGEONS:
        if dungeon_info['name'] == target_dungeon:
            return dungeon_info['min_level'], dungeon_info['max_level']
    return 1, 999


def _normalize_adventurer_score(score: object) -> float:
    """将评分统一归一到 0-10 区间，异常值回退到中位表现。"""
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        score_value = 5.0
    return max(0.0, min(10.0, score_value))


def calculate_expedition_exp_gain(
    adventurer_level: int,
    dungeon_min_level: int,
    dungeon_max_level: int,
    score: object = None,
    has_special_event: bool = False,
    is_extreme_crisis: bool = False,
    berserk_mode: bool = False
) -> int:
    """按地牢等级和 0-10 评分口径计算远征经验。

    berserk_mode 为 True 时，在常规结算之上叠加 BERSERK_EXP_MULTIPLIER 倍率（赛季末狂暴）。
    """
    eval_score = _normalize_adventurer_score(score)
    eval_multiplier = 0.5 + (eval_score / 10.0) * 0.8

    anchor_level = dungeon_min_level
    base_exp_gain = (anchor_level * 100) / 5.0
    exp_gain = int(base_exp_gain * eval_multiplier)

    if adventurer_level < dungeon_min_level:
        level_diff = dungeon_min_level - adventurer_level
        exp_gain = int(exp_gain * (1.0 + (level_diff * 0.5)))
    elif adventurer_level > dungeon_max_level:
        level_diff = adventurer_level - dungeon_max_level
        if level_diff >= 3:
            exp_gain = 5
        else:
            exp_gain = int(exp_gain * max(0.1, 1.0 - (level_diff * 0.3)))

    if has_special_event:
        exp_gain = int(exp_gain * (3.0 if is_extreme_crisis else 2.0))

    if berserk_mode:
        exp_gain = int(exp_gain * BERSERK_EXP_MULTIPLIER)

    return max(0, exp_gain)


# ── 商店定价 ──────────────────────────────────────────────────────────────────

def _random_price_by_rarity(rarity):
    """根据稀有度返回一个随机定价。未知 rarity 退回到 common 区间，避免低估高稀有度装备。"""
    low, high = RARITY_PRICE_RANGES.get(rarity, RARITY_PRICE_RANGES["common"])
    return random.randint(low, high)


# ── 通用数值安全转换 ──────────────────────────────────────────────────────────

def _coerce_int(value, default: int = 0) -> int:
    """将大模型返回的数值安全转成 int，兼容字符串和浮点文本。"""
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _coerce_float(value, default: float = 0.0) -> float:
    """将大模型返回的数值安全转成有限 float，兼容字符串、NaN 和 Infinity。"""
    if value in (None, ''):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _coerce_config_bool(value, default: bool = False) -> bool:
    """将配置项安全转换为布尔值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _get_adventurer_ai_request_timeout(config_key: str, default_seconds: int) -> int:
    """统一读取深渊酒馆 AI 请求超时配置，避免不同阶段各自写死超时。"""
    return max(30, int(current_app.config.get(config_key, default_seconds) or default_seconds))


# ── 文本清理工具（被 shop 和 prompt 共用） ─────────────────────────────────────

def _clean_compact_text(value, max_length: int = 220) -> str:
    """压缩并清理 AI 返回的短文本，避免夹带无意义噪声。"""
    text = str(value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text[:max_length].strip()


def _clean_persisted_text(value) -> str:
    """清理要写库保真的文本，只去噪，不做长度截断。"""
    text = str(value or '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _to_clean_string_list(values, max_items: int, max_item_length: int = 80) -> list[str]:
    """把任意输入安全转成短字符串数组。"""
    if not isinstance(values, list):
        return []

    result = []
    seen = set()
    for value in values:
        text = _clean_compact_text(value, max_item_length)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


# ── AI 响应解析工具（被 prompt 和 parser 共用） ───────────────────────────────────

def _strip_markdown_code_fence(text: str) -> str:
    """移除最外层 markdown 代码块包装。"""
    cleaned = (text or '').strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```[a-zA-Z0-9_-]*\s*', '', cleaned, count=1).strip()
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3].strip()
    return cleaned


def _safe_json_loads(json_str: str) -> object | None:
    """安全解析 JSON，失败时尝试修复 LLM 常见格式问题（尾逗号等）。"""
    if not json_str or not isinstance(json_str, str):
        return None
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        pass
    fixed = re.sub(r',(\s*[}\]])', r'\1', json_str)
    try:
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_json_object_from_text(text: str) -> str | None:
    """从文本中提取首个完整 JSON 对象。"""
    cleaned = _strip_markdown_code_fence(text)
    if cleaned.startswith('json'):
        cleaned = cleaned[4:].strip()

    try:
        parsed = _safe_json_loads(cleaned)
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    start_idx = cleaned.find('{')
    if start_idx == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for idx in range(start_idx, len(cleaned)):
        char = cleaned[idx]
        if in_string:
            if escape_next:
                escape_next = False
            elif char == '\\':
                escape_next = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return cleaned[start_idx:idx + 1]
    return None


def _build_ai_output_preview(text: str, max_length: int = 240) -> str:
    """构建 AI 原始输出预览，便于日志排查。"""
    preview = (text or '').strip().replace('\r', ' ').replace('\n', ' ')
    preview = re.sub(r'\s+', ' ', preview)
    if len(preview) > max_length:
        return preview[:max_length] + '...'
    return preview


def _extract_ai_message_text(response_payload: dict | None) -> str:
    """从 AI 响应中提取首选文本内容，兼容空结构。"""
    if not isinstance(response_payload, dict):
        return ''

    choices = response_payload.get('choices')
    if not isinstance(choices, list) or not choices:
        return ''

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ''

    message = first_choice.get('message')
    if not isinstance(message, dict):
        return ''

    content = message.get('content')
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''

    parts: list[str] = []
    for chunk in content:
        if isinstance(chunk, str):
            parts.append(chunk)
            continue
        if isinstance(chunk, dict):
            chunk_text = chunk.get('text')
            if isinstance(chunk_text, str):
                parts.append(chunk_text)
    return ''.join(parts)


def _build_ai_response_summary(status_code: int | None, response_payload: dict | None) -> str:
    """构建 AI 响应摘要，便于排查空输出、截断和结构变化。"""
    choices = response_payload.get('choices') if isinstance(response_payload, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get('message') if isinstance(first_choice, dict) else {}
    usage = response_payload.get('usage') if isinstance(response_payload, dict) else None
    content = _extract_ai_message_text(response_payload)

    reasoning_content = ''
    if isinstance(message, dict):
        raw_reasoning = message.get('reasoning_content')
        if isinstance(raw_reasoning, str):
            reasoning_content = raw_reasoning
        elif isinstance(raw_reasoning, list):
            reasoning_parts: list[str] = []
            for chunk in raw_reasoning:
                if isinstance(chunk, str):
                    reasoning_parts.append(chunk)
                elif isinstance(chunk, dict) and isinstance(chunk.get('text'), str):
                    reasoning_parts.append(chunk.get('text'))
            reasoning_content = ''.join(reasoning_parts)

    usage_summary = {}
    if isinstance(usage, dict):
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if usage.get(key) is not None:
                usage_summary[key] = usage.get(key)

    summary = {
        'status_code': status_code,
        'choices_count': len(choices) if isinstance(choices, list) else 0,
        'finish_reason': first_choice.get('finish_reason') if isinstance(first_choice, dict) else None,
        'message_keys': sorted(message.keys()) if isinstance(message, dict) else [],
        'content_length': len((content or '').strip()),
        'reasoning_length': len((reasoning_content or '').strip()),
        'usage': usage_summary,
        'content_preview': _build_ai_output_preview(content, max_length=120),
        'reasoning_preview': _build_ai_output_preview(reasoning_content, max_length=120),
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
