"""深渊酒馆 S1 探险配置：S1 专属地牢档位（与原版 adventurer 解耦）。

原版 `DUNGEONS` 档位偏陡：min_level 为 1,2,4,6,8,10,15,20，低段挤在一起、高段又一次
跳 5 级，且 15 级以上集中在末两档。S1 暂定 15 级上限（`ADVENTURER_S1_MAX_LEVEL`），
这里把 8 个主题档位在 1~15 级之间按固定 2 级间距均匀铺开（min_level 1,3,5,7,9,11,13,
15），让升级过程中的副本难度过渡更平滑，且每一档都能在 15 级上限内被推荐到。

之所以单独建一份而不是改原版 `DUNGEONS`：原版那份还被其 `DUNGEON_RANK_MAP` / 组队
rank 匹配逻辑依赖，改动会牵连原版插件；S1 想独立调档位、独立定上限，故自带一份数据。

经验值曲线（`get_adventurer_exp_required`）与单次探险经验（`calculate_expedition_exp_gain`）
仍复用原版纯函数，S1 只在此覆盖地牢档位数据。

自动组队的「跨度加入逻辑」也复用原版那套：把地牢按 min_level 归并成离散阶级（rank），
冒险者按等级算出自己的锚定 rank，再用 rank 差控制能加入哪些档位的小队、概率多大。
这里基于 S1 专属的 `S1_DUNGEONS`（8 档、2 级等间距）建一份独立的 rank 映射与对应工具
函数，与原版 `adventurer_config_service` 的 `DUNGEON_RANK_MAP` 等解耦，避免牵连旧插件。
"""

import math
import random
from flask import current_app

# 8 个主题档位，min_level 固定 2 级间距铺开到 15 级上限；max_level 与下一档 min 衔接，
# 形成轻微重叠让难度过渡平滑。末档为 15+（max_level 999）。
#
# 命名遵循「世界尺度逐级递进」原则，避免低等级就跳到深渊/屠龙/灭世的违和感：
# 野外杂兵 → 荒废遗迹 → 险境兽巢 → 妖魔据点 → 古代秘境 → 巨龙/恶魔 → 魔王城 → 真正的深渊。
# 「深渊」概念刻意压到末档（15+）作为终局，呼应「深渊酒馆」主题，不在中段提前出现。
S1_DUNGEONS = [
    # Lv 1-3 · 野外杂兵（哥布林 / 野兽 / 盗匪）
    {'name': '哥布林矿坑', 'context_key': 'goblin_mine', 'min_level': 1, 'max_level': 3},
    {'name': '野狼林道', 'context_key': 'wolfwood_road', 'min_level': 1, 'max_level': 3},
    {'name': '荒野哨站', 'context_key': 'wilderness_outpost', 'min_level': 1, 'max_level': 3},

    # Lv 3-5 · 荒废遗迹（亡灵 / 邪教 / 毒沼）
    {'name': '迷雾沼泽', 'context_key': 'mist_swamp', 'min_level': 3, 'max_level': 5},
    {'name': '叹息地牢', 'context_key': 'sighing_dungeon', 'min_level': 3, 'max_level': 5},
    {'name': '废弃修道院', 'context_key': 'abandoned_monastery', 'min_level': 3, 'max_level': 5},

    # Lv 5-7 · 险境兽巢（猛兽 / 元素 / 毒窟）
    {'name': '剧毒钟乳洞', 'context_key': 'venom_stalactite_cave', 'min_level': 5, 'max_level': 7},
    {'name': '狂沙荒漠', 'context_key': 'raging_sands', 'min_level': 5, 'max_level': 7},
    {'name': '风暴尖塔', 'context_key': 'storm_spire', 'min_level': 5, 'max_level': 7},

    # Lv 7-9 · 妖魔据点（妖魔 / 食人魔 / 邪术）
    {'name': '暗影迷宫', 'context_key': 'shadow_maze', 'min_level': 7, 'max_level': 9},
    {'name': '冰霜峡谷', 'context_key': 'frost_canyon', 'min_level': 7, 'max_level': 9},
    {'name': '食人魔要塞', 'context_key': 'ogre_fortress', 'min_level': 7, 'max_level': 9},

    # Lv 9-11 · 古代秘境（巫妖 / 远古守卫）
    {'name': '遗忘废墟', 'context_key': 'forgotten_ruins', 'min_level': 9, 'max_level': 11},
    {'name': '熔岩火山', 'context_key': 'lava_volcano', 'min_level': 9, 'max_level': 11},
    {'name': '古王陵寝', 'context_key': 'ancient_king_tomb', 'min_level': 9, 'max_level': 11},

    # Lv 11-13 · 巨兽领域（巨龙 / 恶魔）
    {'name': '巨龙巢穴', 'context_key': 'dragon_lair', 'min_level': 11, 'max_level': 13},
    {'name': '恶魔岛', 'context_key': 'demon_island', 'min_level': 11, 'max_level': 13},
    {'name': '幽冥王都', 'context_key': 'nether_capital', 'min_level': 11, 'max_level': 13},

    # Lv 13-15 · 终局前高危生态（日蚀王庭 / 星界遗城 / 虚空神殿）
    {'name': '黑日王庭', 'context_key': 'black_sun_court', 'min_level': 13, 'max_level': 15},
    {'name': '陨落星都', 'context_key': 'fallen_star_city', 'min_level': 13, 'max_level': 15},
    {'name': '虚空神殿', 'context_key': 'void_temple', 'min_level': 13, 'max_level': 15},

    # Lv 15+ · 终末深渊（终局，呼应「深渊酒馆」主题）
    {'name': '哀嚎深渊', 'context_key': 'wailing_abyss', 'min_level': 15, 'max_level': 999},
    {'name': '深渊核心', 'context_key': 'abyss_core', 'min_level': 15, 'max_level': 999},
    {'name': '万魔殿', 'context_key': 'pandemonium', 'min_level': 15, 'max_level': 999},
]

# 当前对玩家开放/自动生成的常规副本池。Lv.15+ 终局档先保留为历史/后台配置，
# 不进入自动组队、委托生成、追查 Boss 生成等玩家可触达入口。
S1_ACTIVE_DUNGEONS = [d for d in S1_DUNGEONS if d.get('max_level', 999) < 999]
S1_ACTIVE_DUNGEON_NAMES = frozenset(d['name'] for d in S1_ACTIVE_DUNGEONS)
S1_ACTIVE_MAX_LEVEL = max(d['max_level'] for d in S1_ACTIVE_DUNGEONS)

# ── 隐藏副本（只由常规副本发车时独立触发，不进入组队/选图池） ────────────────
# drops 从第一版起就是数组：后续可为同一隐藏副本增加不同稀有度、不同数量的独立掉落。
# 同一常规副本可配置多个隐藏副本；10% 总入口判定命中后再按 selection_weight 选一个。
# level_offset=None 表示继承全局默认难度，单项可直接覆盖为非负整数。
def _hidden_dungeon(
    code: str,
    source_dungeon: str,
    name: str,
    drops: list[dict],
    *,
    enabled: bool = True,
    selection_weight: float = 1.0,
    level_offset: int | None = None,
) -> dict:
    return {
        'code': code,
        'source_dungeon': source_dungeon,
        'name': name,
        'enabled': enabled,
        'selection_weight': selection_weight,
        'level_offset': level_offset,
        'drops': drops,
    }


S1_HIDDEN_DUNGEONS = [
    _hidden_dungeon('black_iron_private_mine', '哥布林矿坑', '黑铁私矿', [{'item_name': '黑铁母矿', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '从黑铁私矿最深处剥出的致密母矿，适合作为高阶锻造材料。'}]),
    _hidden_dungeon('subterranean_azure_tide', '哥布林矿坑', '地心蓝潮海', [{'item_name': '蓝潮矿膜', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '地心潮水掠过矿物喷口后，在岩面凝成的蓝色柔韧薄膜。'}], selection_weight=0.5),
    _hidden_dungeon('gloomwood', '野狼林道', '幽暗密林', [{'item_name': '月蚀狼髓', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '月蚀之夜凝在狼王脊骨中的银灰髓质。'}]),
    _hidden_dungeon('starlit_firefly_valley', '野狼林道', '星萤花谷', [{'item_name': '星萤花露', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '星萤掠过夜放花群时，在花瓣上凝成的微光露液。'}], selection_weight=0.5),
    _hidden_dungeon('bannerless_fort', '荒野哨站', '无旗军堡', [{'item_name': '无旗陨铁', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '无名军团从天外陨石中锻下的沉重铁料。'}]),
    _hidden_dungeon('beast_migration_valley', '荒野哨站', '千兽迁徙谷', [{'item_name': '迁徙角纹', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '巨兽长途迁徙时自然磨落的角质纹片，层纹记录着水草与季候。'}], selection_weight=0.5),
    _hidden_dungeon('sunken_altar', '迷雾沼泽', '沉没祭坛', [{'item_name': '沉祭雾珠', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '沉没祭坛长年吸纳沼雾后结成的浑圆珠核。'}]),
    _hidden_dungeon('mirror_lotus_water_garden', '迷雾沼泽', '镜莲水庭', [{'item_name': '镜莲清露', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '镜莲叶心汇聚的澄澈露液，静置时会映出水庭的层叠倒影。'}], selection_weight=0.5),
    _hidden_dungeon('silent_execution_ground', '叹息地牢', '无声刑场', [{'item_name': '缄声锁芯', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '封死哀号与秘密的古旧锁芯，仍残留压制声息的力量。'}]),
    _hidden_dungeon('echo_organ_cavern', '叹息地牢', '回声风琴洞', [{'item_name': '风琴鸣晶', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '洞风穿过石管时在共鸣节点析出的空腔晶体，轻触便有余音。'}], selection_weight=0.5),
    _hidden_dungeon('holy_reliquary_catacomb', '废弃修道院', '圣骸墓窟', [{'item_name': '圣骸磷晶', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '圣骸周围缓慢生长的淡金磷晶。'}]),
    _hidden_dungeon('wax_sealed_penitence_hall', '废弃修道院', '蜡封忏悔堂', [{'item_name': '誓蜡封核', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '誓言残响在层层冷蜡中压成的密实封核，受热时显出互不重合的旧纹。'}], selection_weight=0.5),
    _hidden_dungeon('serpent_matriarch_palace', '剧毒钟乳洞', '蛇母蜕宫', [{'item_name': '蛇母晶蜕', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '蛇母历经漫长蜕变留下的透明晶壳。'}]),
    _hidden_dungeon('living_fungal_lung', '剧毒钟乳洞', '活体菌肺', [{'item_name': '孢压菌囊', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '厚壁菌膜包裹的弹性孢囊，囊纹随内外压力差轻微收缩。'}], selection_weight=0.5),
    _hidden_dungeon('scorpion_king_catacomb', '狂沙荒漠', '蝎王地宫', [{'item_name': '蝎王尾晶', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '蝎王尾针内凝成的剧毒晶体。'}]),
    _hidden_dungeon('azure_mirage_sea', '狂沙荒漠', '碧落蜃海', [{'item_name': '蜃海琉砂', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '碧落蜃海热浪与泉汽反复熔蚀后留下的蓝金色琉璃砂。'}], selection_weight=0.5),
    _hidden_dungeon('eye_of_thunder', '风暴尖塔', '雷霆之眼', [{'item_name': '雷眼晶核', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '风暴核心短暂平静时析出的雷光晶核。'}]),
    _hidden_dungeon('cloudtop_flower_garden', '风暴尖塔', '云上花庭', [{'item_name': '云庭虹羽', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '云上花庭高空生灵换落的轻羽，羽脉中留有雨后虹光。'}], selection_weight=0.5),
    _hidden_dungeon('faceless_gallery', '暗影迷宫', '无面回廊', [{'item_name': '无面镜银', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '无法映出持有者面孔的流动镜银。'}]),
    _hidden_dungeon('daynight_boundary_court', '暗影迷宫', '昼夜交界庭', [{'item_name': '曦影鳞粉', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '昼夜光界掠过影蛾翅面后脱落的双色鳞粉。'}], selection_weight=0.5),
    _hidden_dungeon('eternal_frost_royal_tomb', '冰霜峡谷', '永冻王墓', [{'item_name': '永冻王髓', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '永冻王墓中千年不化的冰蓝骨髓。'}]),
    _hidden_dungeon('aurora_mirror_lake', '冰霜峡谷', '极光镜湖', [{'item_name': '极光冰绡', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '极光映入层冰后析出的柔韧冰晶薄片，触之如冷绡。'}], selection_weight=0.5),
    _hidden_dungeon('blood_feast_cellar', '食人魔要塞', '血宴地窖', [{'item_name': '巨人战骨', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '经无数战役淬炼、仍带裂痕的巨人遗骨。'}]),
    _hidden_dungeon('bonewheel_siege_factory', '食人魔要塞', '骨轮攻城厂', [{'item_name': '骨轮轴髓', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '骨质主轴长期受压后凝成的油润髓芯，断面保留交错的受力纹。'}], selection_weight=0.5),
    _hidden_dungeon('chaos_throne', '遗忘废墟', '混沌王座', [{'item_name': '混沌王座残片', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '从混沌王座崩落的碎片，纹理会随注视而改变。'}]),
    _hidden_dungeon('self_rewriting_quarter', '遗忘废墟', '自我改写街区', [{'item_name': '重排界核', 'type': 'material', 'rarity': 'legendary', 'quantity': 1, 'description': '相邻空间反复错接后留下的边界凝核，断面间的纹路会短暂错位再合拢。'}], selection_weight=0.5),
    _hidden_dungeon('earthfire_molten_heart', '熔岩火山', '地火熔心', [{'item_name': '熔心明焰', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '地火熔心中不会熄灭的一簇澄明火种。'}]),
    _hidden_dungeon('glass_storm_stratum', '熔岩火山', '玻璃风暴层', [{'item_name': '熔蚀风晶', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '急冷熔滴在反复切变的热风中凝成的薄刃晶体，内部封有细密气纹。'}], selection_weight=0.5),
    _hidden_dungeon('inverted_imperial_tomb', '古王陵寝', '倒悬帝陵', [{'item_name': '倒悬龙玉', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '倒悬帝陵中逆着重力生长的龙纹玉髓。'}]),
    _hidden_dungeon('starvault_funeral_bloom', '古王陵寝', '星穹葬花原', [{'item_name': '星葬花芯', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '葬花随地下星图完成一轮开合后留下的微光花芯。'}], selection_weight=0.5),
    _hidden_dungeon('dragon_emperor_tomb', '巨龙巢穴', '龙皇陵', [{'item_name': '龙皇遗鳞', 'type': 'material', 'rarity': 'legendary', 'quantity': 1, 'description': '龙皇遗骸上尚存威压的一枚古鳞。'}]),
    _hidden_dungeon('dragon_molt_compost', '巨龙巢穴', '龙蜕腐殖场', [{'item_name': '腐鳞菌壳', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '菌索沿陈旧鳞片矿化后形成的薄壳，孔隙中残留温热的腐殖气息。'}], selection_weight=0.5),
    _hidden_dungeon('prison_of_ten_thousand_sins', '恶魔岛', '万罪监牢', [{'item_name': '断罪锁芯', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '万罪监牢用来锁住恶魔真名的刑锁核心。'}]),
    _hidden_dungeon('reverse_tide_hatchery', '恶魔岛', '逆潮孵化滩', [{'item_name': '逆潮卵膜', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '卵囊脱落的层叠韧膜，内侧细纹保留逆向潮流冲刷的痕迹。'}], selection_weight=0.5),
    _hidden_dungeon('twilight_of_the_gods', '幽冥王都', '诸神黄昏', [{'item_name': '黄昏魂露', 'type': 'material', 'rarity': 'epic', 'quantity': 1, 'description': '神性余晖消散前凝结的一滴魂露。'}]),
    _hidden_dungeon('soul_coin_foundry', '幽冥王都', '亡魂铸币厂', [{'item_name': '冥铸魂渣', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '冥铸余热散去后留下的暗淡熔渣，内部夹着无法独立成句的记忆回声。'}], selection_weight=0.5),
    _hidden_dungeon('infernal_demon_city', '黑日王庭', '炼狱魔城', [{'item_name': '炼狱王心', 'type': 'material', 'rarity': 'legendary', 'quantity': 1, 'description': '炼狱王者陨落后仍在跳动的炽热核心。'}]),
    _hidden_dungeon('shadowless_lightwell', '黑日王庭', '无影蚀光井', [{'item_name': '蚀光煤晶', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '吸光沉积压成的暗色煤晶，光照下仅在断面边缘显出细小亮纹。'}], selection_weight=0.5),
    _hidden_dungeon('space_time_rift', '陨落星都', '时空裂缝', [{'item_name': '时隙星砂', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '从时空裂隙边缘刮落、在掌中明灭的星砂。'}]),
    _hidden_dungeon('weightless_wreckage_ring', '陨落星都', '失重残骸环', [{'item_name': '零重星壳', 'type': 'material', 'rarity': 'legendary', 'quantity': 1, 'description': '长久悬浮在引力交界的星体薄壳，移动时仍带微弱的惯性迟滞。'}], selection_weight=0.5),
    _hidden_dungeon('tower_of_creation', '虚空神殿', '创世神塔', [{'item_name': '创世净尘', 'type': 'material', 'rarity': 'legendary', 'quantity': 1, 'description': '创世神塔最初一束光落定后留下的纯净微尘。'}]),
    _hidden_dungeon('law_scrap_yard', '虚空神殿', '法则废料场', [{'item_name': '废律刻片', 'type': 'material', 'rarity': 'rare', 'quantity': 1, 'description': '校准失效的规则构件剥落后留下的刻片，边缘铭痕残缺而不连续。'}], selection_weight=0.5),
]

def _validate_s1_hidden_dungeons() -> None:
    if len({d['code'] for d in S1_HIDDEN_DUNGEONS}) != len(S1_HIDDEN_DUNGEONS):
        raise ValueError('S1 隐藏副本 code 必须唯一')
    if len({d['name'] for d in S1_HIDDEN_DUNGEONS}) != len(S1_HIDDEN_DUNGEONS):
        raise ValueError('S1 隐藏副本 name 必须唯一')
    for row in S1_HIDDEN_DUNGEONS:
        code = str(row.get('code') or '')
        if (
            not code
            or not code.isascii()
            or not code[0].isalpha()
            or code != code.lower()
            or not all(char.isalnum() or char == '_' for char in code)
        ):
            raise ValueError(f'S1 隐藏副本 code 非法：{code!r}')
        if row.get('source_dungeon') not in S1_ACTIVE_DUNGEON_NAMES:
            raise ValueError(f'S1 隐藏副本来源未开放：{row.get("source_dungeon")!r}')
        if not str(row.get('name') or '').strip():
            raise ValueError(f'S1 隐藏副本 {code} 缺少展示名')
        if not isinstance(row.get('enabled'), bool):
            raise ValueError(f'S1 隐藏副本 {code} 的 enabled 必须是布尔值')
        weight = row.get('selection_weight')
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                or not math.isfinite(weight) or weight < 0:
            raise ValueError(f'S1 隐藏副本 {code} 的 selection_weight 必须是非负有限数')
        level_offset = row.get('level_offset')
        if level_offset is not None and (
            isinstance(level_offset, bool)
            or not isinstance(level_offset, int)
            or level_offset < 0
        ):
            raise ValueError(f'S1 隐藏副本 {code} 的 level_offset 必须是非负整数或 None')
        if not isinstance(row.get('drops'), list):
            raise ValueError(f'S1 隐藏副本 {code} 的 drops 必须是数组')


_validate_s1_hidden_dungeons()

S1_HIDDEN_DUNGEON_BY_CODE = {d['code']: d for d in S1_HIDDEN_DUNGEONS}
S1_HIDDEN_DUNGEON_BY_NAME = {d['name']: d for d in S1_HIDDEN_DUNGEONS}
_hidden_dungeons_by_source: dict[str, list[dict]] = {}
for _hidden in S1_HIDDEN_DUNGEONS:
    _hidden_dungeons_by_source.setdefault(_hidden['source_dungeon'], []).append(_hidden)
S1_HIDDEN_DUNGEONS_BY_SOURCE = {
    source: tuple(rows) for source, rows in _hidden_dungeons_by_source.items()
}


S1_HIDDEN_RELIC_TITLE_CODE = 'hidden_relic_collector'
S1_HIDDEN_RELIC_TITLE_RECIPE_ID = 'hidden_relic_title'
# 精确绑定掉落名称；同一隐藏后来增加其它掉落时，不改变既有称号配方。
S1_HIDDEN_RELIC_TITLE_MATERIALS = (
    ('dragon_emperor_tomb', '龙皇遗鳞'),
    ('infernal_demon_city', '炼狱王心'),
    ('tower_of_creation', '创世净尘'),
    ('self_rewriting_quarter', '重排界核'),
    ('weightless_wreckage_ring', '零重星壳'),
    ('wax_sealed_penitence_hall', '誓蜡封核'),
    ('bonewheel_siege_factory', '骨轮轴髓'),
    ('glass_storm_stratum', '熔蚀风晶'),
    ('prison_of_ten_thousand_sins', '断罪锁芯'),
    ('twilight_of_the_gods', '黄昏魂露'),
    ('inverted_imperial_tomb', '倒悬龙玉'),
)


def get_s1_hidden_relic_title_ingredients() -> tuple[dict, ...]:
    """十一张不同常规地图的固定藏品；兑换数量与预览共用插件配置。"""
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    quantities = {}
    for rarity in ('legendary', 'epic'):
        key = f'ADVENTURER_S1_HIDDEN_RELIC_TITLE_{rarity.upper()}_QUANTITY'
        value = current_app.config.get(key, AdventurerS1Plugin.default_config[key])
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'{key} 必须为正整数')
        quantities[rarity] = value
    ingredients = []
    for code, item_name in S1_HIDDEN_RELIC_TITLE_MATERIALS:
        hidden = S1_HIDDEN_DUNGEON_BY_CODE[code]
        matching_drops = [drop for drop in hidden['drops'] if drop.get('item_name') == item_name]
        if len(matching_drops) != 1:
            raise ValueError(f'称号藏品配置必须唯一匹配掉落：{code}/{item_name}')
        drop = matching_drops[0]
        ingredients.append({
            'item_name': drop['item_name'],
            'rarity': drop['rarity'],
            'quantity': quantities[drop['rarity']],
            'source_dungeon': hidden['source_dungeon'],
            'hidden_dungeon': hidden['name'],
        })
    return tuple(ingredients)


def get_s1_hidden_dungeons_by_source(
    source_dungeon: str | None,
    *,
    include_disabled: bool = False,
) -> tuple[dict, ...]:
    rows = S1_HIDDEN_DUNGEONS_BY_SOURCE.get(str(source_dungeon or '').strip(), ())
    if include_disabled:
        return rows
    return tuple(row for row in rows if row.get('enabled') is True)


def get_s1_hidden_dungeon_by_source(source_dungeon: str | None) -> dict | None:
    """兼容旧调用方：返回该入口首个已启用且权重大于 0 的隐藏副本。"""
    return next((
        row for row in get_s1_hidden_dungeons_by_source(source_dungeon)
        if float(row.get('selection_weight') or 0) > 0
    ), None)


def get_s1_hidden_dungeon_by_code(code: str | None) -> dict | None:
    """按稳定代码读取定义；包含停用项，供历史日志兼容与后续迁移使用。"""
    return S1_HIDDEN_DUNGEON_BY_CODE.get(str(code or '').strip())


def get_s1_hidden_dungeon(target_dungeon: str | None) -> dict | None:
    return S1_HIDDEN_DUNGEON_BY_NAME.get(str(target_dungeon or '').strip())


def _get_s1_hidden_dungeon_level_offset(hidden: dict | None = None) -> int:
    if isinstance(hidden, dict) and hidden.get('level_offset') is not None:
        return max(0, int(hidden['level_offset']))
    return max(0, int(current_app.config.get(
        'ADVENTURER_S1_HIDDEN_DUNGEON_LEVEL_OFFSET', 2,
    ) or 0))


def get_s1_active_dungeons() -> list[dict]:
    """当前玩家可触达的常规 S1 副本列表。"""
    return list(S1_ACTIVE_DUNGEONS)


def compute_s1_special_event_probability(dungeon_min_level: int) -> float:
    """按 S1 当前开放上限平滑计算特殊事件概率。

    旧 legacy 公式在 Lv.9 起封顶 25%，导致 9/11/13/15 档没有高阶差异。
    S1 当前开放最高常规档为 Lv.13-15，因此按 1~15 线性插值到 15%。
    """
    level = max(1, int(dungeon_min_level or 1))
    max_level = max(1, int(S1_ACTIVE_MAX_LEVEL or 15))
    clamped = min(level, max_level)
    min_prob = 0.01
    max_prob = 0.15
    if max_level <= 1:
        return max_prob
    progress = (clamped - 1) / (max_level - 1)
    return min_prob + (max_prob - min_prob) * progress


S1_SPECIAL_EVENT_RISK_MULTIPLIERS = {
    'local_anomaly': 1.6,
    'regional_incident': 2.0,
    'world_fact_catalyst': 2.4,
    'extreme_crisis': 3.0,
    'stranger': 3.0,
    'abyss': 3.2,
    'unbeatable_horror': 3.5,
}

# 特殊核心的能力画像等级只负责给大纲与主剧情因果底稿提供共同数值参照，
# 与经验倍率、伤亡结果和最终伤害结算彼此独立。具体值可由部署配置覆盖。
S1_SPECIAL_CORE_LEVEL_OFFSET_DEFAULTS = {
    'normal': 1,
    'local_anomaly': 2,
    'regional_incident': 3,
    'world_fact_catalyst': 4,
    'extreme_crisis': 5,
    'stranger': 5,
    'abyss': 6,
    'unbeatable_horror': 8,
    'hidden_boss': 3,
}

S1_SPECIAL_EVENT_PRESENTATION_THEMES = frozenset(S1_SPECIAL_EVENT_RISK_MULTIPLIERS)

S1_SPECIAL_EVENT_WARNING_BY_THEME = {
    'local_anomaly': "✨【局部异象：地牢深处传来不合常理的回响，熟悉的道路正在长出陌生的岔口。】✨",
    'regional_incident': "🌩️【区域异变：躁动已越过地牢边界，周边势力与聚落正被同一场风暴卷入。】🌩️",
    'world_fact_catalyst': "🌐【世界涟漪：一枚足以撬动旧有格局的楔子已经落下，远方也将听见回声。】🌐",
    'extreme_crisis': "☄️【极端危机：常规法则正在崩裂，一场足以改写队伍命运的灾厄已经逼近。】☄️",
    'stranger': "👁️【陌生来客：群星之外的目光落向此地，它的善意与恶意都无法用常理衡量。】👁️",
    'abyss': "🕳️【深渊降临：裂隙在前路睁开，任何迟疑、贪念与失误都可能被放大成无法挽回的代价。】🕳️",
    'unbeatable_horror': "☠️【绝对死境：前方的存在并非猎物；仅仅与之照面，便足以让整支队伍从地图上消失。】☠️",
}
S1_SPECIAL_EVENT_DEFAULT_WARNING = (
    "⚠️【命运偏航：这不再是一次循规蹈矩的探险，未知的机遇与代价正同时逼近。】⚠️"
)
S1_SPECIAL_EVENT_WARNING_TEXTS = frozenset(S1_SPECIAL_EVENT_WARNING_BY_THEME.values()) | {
    S1_SPECIAL_EVENT_DEFAULT_WARNING,
}


def get_s1_special_event_warning(theme: str | None) -> str:
    """Return the fixed player-facing marker for one structured event theme."""
    return S1_SPECIAL_EVENT_WARNING_BY_THEME.get(
        str(theme or '').strip(),
        S1_SPECIAL_EVENT_DEFAULT_WARNING,
    )


def resolve_s1_special_event_kind(full_log: dict | None) -> str:
    """Return the single current AI-facing special-event category.

    Stored logs keep the legacy trigger/type/risk fields for compatibility, but
    current AI prompts must not receive those overlapping concepts separately.
    """
    if not isinstance(full_log, dict):
        return 'normal'
    current_kind = str(full_log.get('special_event_kind') or '').strip()
    if current_kind == 'normal' or current_kind in S1_SPECIAL_EVENT_PRESENTATION_THEMES:
        return current_kind
    if not full_log.get('has_special_event'):
        return 'normal'
    event_type = str(full_log.get('special_event_type') or '').strip()
    if event_type in S1_SPECIAL_EVENT_PRESENTATION_THEMES:
        return event_type
    risk_tier = str(full_log.get('special_event_risk_tier') or '').strip()
    if risk_tier in S1_SPECIAL_EVENT_PRESENTATION_THEMES:
        return risk_tier
    if full_log.get('is_extreme_crisis'):
        return 'extreme_crisis'
    # Legacy special-event logs may predate the risk-tier field. Use the least
    # expansive current category instead of exposing another AI-facing alias.
    return 'local_anomaly'


def get_s1_high_risk_adjudication_sources(
    full_log: dict | None,
) -> tuple[str, ...]:
    """Return the closed structured trigger set for high-risk adjudication."""
    if not isinstance(full_log, dict):
        return ()

    sources: list[str] = []
    if (
        full_log.get('has_special_event') is True
        or full_log.get('is_extreme_crisis') is True
        or resolve_s1_special_event_kind(full_log) != 'normal'
    ):
        sources.append('special_event')

    hidden_boss_context = full_log.get('hidden_boss_context')
    if isinstance(hidden_boss_context, dict) and hidden_boss_context:
        sources.append('hidden_boss')

    # Formal campaign challenges are frozen at departure. Discovery and ordinary
    # stage tasks do not expose a challenge and must keep their existing route.
    dispatch = full_log.get('campaign_dispatch')
    if isinstance(dispatch, dict) and dispatch.get('discovery') is False:
        task = dispatch.get('stage_task')
        context = dispatch.get('challenge_context')
        challenge = context.get('challenge') if isinstance(context, dict) else None
        opponents = challenge.get('opponents') if isinstance(challenge, dict) else None
        if isinstance(task, dict) and task and isinstance(opponents, list) and opponents:
            sources.append('campaign_challenge')
    return tuple(sources)


def requires_s1_high_risk_adjudication(full_log: dict | None) -> bool:
    """Whether this expedition must complete the high-risk fact pipeline."""
    return bool(get_s1_high_risk_adjudication_sources(full_log))


def resolve_s1_special_event_theme(full_log: dict | None) -> str | None:
    """Return the safe, public UI theme for a structured special-event roll."""
    if not isinstance(full_log, dict) or not full_log.get('has_special_event'):
        return None
    event_type = str(full_log.get('special_event_type') or '').strip()
    if event_type in S1_SPECIAL_EVENT_PRESENTATION_THEMES:
        return event_type
    risk_tier = str(full_log.get('special_event_risk_tier') or '').strip()
    if risk_tier in S1_SPECIAL_EVENT_PRESENTATION_THEMES:
        return risk_tier
    if full_log.get('is_extreme_crisis'):
        return 'extreme_crisis'
    return 'special_event'


def resolve_s1_special_event_event_index(full_log: dict | None) -> int | None:
    """Locate the fixed backend-generated warning marker without judging AI prose."""
    if not isinstance(full_log, dict) or not full_log.get('has_special_event'):
        return None
    stored = full_log.get('special_event_event_index')
    if isinstance(stored, int) and stored >= 0:
        return stored
    for index, event in enumerate(full_log.get('events') or []):
        raw_text = str(event.get('text') if isinstance(event, dict) else event or '').strip()
        if raw_text in S1_SPECIAL_EVENT_WARNING_TEXTS:
            return index
        legacy_text = raw_text.lstrip('💀🚨')
        if legacy_text.startswith('【特殊危机：') or legacy_text.startswith('【深渊警告：'):
            return index
    return None


def classify_s1_special_event_risk(
    dungeon_min_level: int,
    *,
    is_extreme_crisis: bool = False,
    special_event_type: str | None = None,
) -> str | None:
    """Return the S1 special-event reward/difficulty tier for a rolled event."""
    if special_event_type in ('abyss', 'stranger', 'unbeatable_horror'):
        return special_event_type
    if is_extreme_crisis:
        return 'extreme_crisis'

    level = max(1, int(dungeon_min_level or 1))
    if level >= 11:
        return 'world_fact_catalyst'
    if level >= 7:
        return 'regional_incident'
    return 'local_anomaly'


def get_s1_special_event_exp_multiplier(risk_tier: str | None) -> float:
    """Experience multiplier for S1 special-event risk tiers."""
    if not risk_tier:
        return 1.0
    return float(S1_SPECIAL_EVENT_RISK_MULTIPLIERS.get(risk_tier, 2.0))


def get_s1_special_core_basis_level(
    dungeon_min_level: int,
    *,
    special_event_kind: str | None = None,
    has_hidden_boss: bool = False,
    party_highest_level: int | None = None,
) -> int:
    """所有新核心共用队伍与地图下限；既有主体和原生伴生生态不重算。"""
    reference = get_s1_special_core_level_reference(
        dungeon_min_level,
        special_event_kind=special_event_kind,
        has_hidden_boss=has_hidden_boss,
        party_highest_level=party_highest_level,
    )
    return reference['basis_level']


def get_s1_special_core_level_reference(
    dungeon_min_level: int,
    *,
    special_event_kind: str | None = None,
    has_hidden_boss: bool = False,
    party_highest_level: int | None = None,
) -> dict[str, object]:
    """把同一次配置计算的基础下限、事件/Boss提升和最终等级交给生成侧。"""
    level = max(1, int(dungeon_min_level or 1), int(party_highest_level or 1))
    configured = current_app.config.get(
        'ADVENTURER_S1_SPECIAL_CORE_LEVEL_OFFSETS',
        S1_SPECIAL_CORE_LEVEL_OFFSET_DEFAULTS,
    )
    offsets = (
        configured
        if isinstance(configured, dict)
        else S1_SPECIAL_CORE_LEVEL_OFFSET_DEFAULTS
    )

    def resolve_offset(key: str) -> int:
        fallback = S1_SPECIAL_CORE_LEVEL_OFFSET_DEFAULTS[key]
        value = offsets.get(key, fallback)
        if isinstance(value, bool):
            return fallback
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return fallback

    kind = str(special_event_kind or 'normal').strip()
    if kind not in S1_SPECIAL_CORE_LEVEL_OFFSET_DEFAULTS:
        kind = 'normal'
    event_offset = resolve_offset(kind)
    hidden_boss_offset = resolve_offset('hidden_boss') if has_hidden_boss else 0
    offset = max(event_offset, hidden_boss_offset)
    return {
        'base_level': level,
        'special_event_kind': kind,
        'event_offset': event_offset,
        'hidden_boss_offset': hidden_boss_offset,
        'applied_offset': offset,
        'basis_level': level + offset,
    }


# ── 特殊副本（门票制，独立于常规 S1_DUNGEONS 与自动组队） ────────────────────
# 这类副本不进入常规等级档位 / 自动组队 rank 匹配：必须由持有对应门票的玩家手动
# 建队，发车时后端扣票（即便中途死亡也扣），并由后端「逐成员静默掷骰」决定本次机缘
# （是否获得新职业 / 进阶职业 / 领悟新技能 / 拔除负面特质），再把判定结果（含成功与失败）
# 作为指令注入主探险 AI 的 user prompt，由 AI 负责生成具体名称与剧情。门票与掷骰均不下发前端。
#
# 概率与门票来源（深渊拾荒队掉落）目前均为占位，后续平衡时调整；不下发前端。
S1_SPECIAL_DUNGEONS = [
    {
        'name': '转职试炼场',
        'context_key': 'class_trial',
        'ticket_item': '转职试炼凭证',
        'ticket_rarity': 'epic',
        'reward_kind': 'class',          # 后端判定职业获得 / 进阶
        'new_class_prob': 0.50,          # 无职业者获得新职业概率
        'advance_class_prob': 0.25,      # 已有职业者进阶概率
        'level_label': '特殊·不限等级',
        'flavor': '试炼场的守门者只为持「转职试炼凭证」者开启；通过试炼或可确立、淬炼自己的职业。',
    },
    {
        'name': '顿悟秘境',
        'context_key': 'enlightenment_realm',
        'ticket_item': '顿悟之钥',
        'ticket_rarity': 'epic',
        'reward_kind': 'skill',          # 后端判定领悟新技能
        'new_skill_prob': 0.50,          # 领悟新技能概率（占位）
        'level_label': '特殊·不限等级',
        'flavor': '以「顿悟之钥」开启的静谧之地，机缘巧合下冒险者更易在此领悟契合自身的新技能。',
    },
    {
        'name': '祛厄药庐',
        'context_key': 'calamity_relief_clinic',
        'ticket_item': '祛厄引路符',
        'ticket_rarity': 'legendary',
        'reward_kind': 'trait_relief',   # 每名成员：有可根治负面则成功，AI 自选拔除哪一条
        'level_label': '特殊·不限等级',
        'flavor': '藏于深渊褶皱中的古老药庐，庐中坐诊者专接寻常疗养、祈祷难以根治的诅咒与顽疾；持「祛厄引路符」者方可叩门，为每名求治者拔除或根治一项负面特质。',
    },
]

S1_SPECIAL_DUNGEONS.extend([
    {
        'name': '万象裂隙',
        'context_key': 'independent_materials',
        'ticket_item': '裂隙通行证',
        'ticket_rarity': 'epic',
        'reward_kind': 'materials',
        'independent_run': True,
        'scavenge_drop_enabled': False,
        'level_label': '材料秘境',
        'flavor': '裂隙每次开启，都是一场全新的冒险。队伍表现越出色，获得的材料箱越多；每人箱数相同，开出的材料各凭运气。需随身携带裂隙通行证，获取途径暂未开放。',
    },
    {
        'name': '超越之门',
        'context_key': 'independent_advancement',
        'ticket_item': '超越试炼凭证',
        'ticket_rarity': 'legendary',
        'reward_kind': 'level_cap_trial',
        'independent_run': True,
        'scavenge_drop_enabled': False,
        'entry_enabled': False,
        # 仅预留未来超高难试炼配置；当前没有解锁等级上限的消费者。
        'level_cap_from': 15,
        'level_cap_to': 20,
        'level_label': '晋级试炼·尚未开放',
        'flavor': '通往更高境界的试炼之门尚未开启。',
    },
])

S1_SPECIAL_DUNGEON_MAP = {d['name']: d for d in S1_SPECIAL_DUNGEONS}


def is_s1_independent_dungeon(target_dungeon: str | None) -> bool:
    """配置权威：场次不读写个人或世界剧情记忆，结算流水仍保留。"""
    return bool(S1_SPECIAL_DUNGEON_MAP.get(target_dungeon, {}).get('independent_run'))


def _is_s1_dungeon_context_key(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value.isascii()
        and value == value.lower()
        and value[0].isalpha()
        and all(char.isalnum() or char == '_' for char in value)
    )


def _validate_s1_dungeon_context_keys() -> None:
    """展示名可迁移，context_key/code 一经发布必须保持稳定。"""
    keyed_rows = [
        *((row['name'], row.get('context_key')) for row in S1_DUNGEONS),
        *((row['name'], row.get('context_key')) for row in S1_SPECIAL_DUNGEONS),
        *((row['name'], row.get('code')) for row in S1_HIDDEN_DUNGEONS),
    ]
    keys = [key for _, key in keyed_rows]
    if any(not _is_s1_dungeon_context_key(key) for key in keys):
        invalid = [(name, key) for name, key in keyed_rows if not _is_s1_dungeon_context_key(key)]
        raise ValueError(f'S1 副本 context key 非法：{invalid!r}')
    if len(set(keys)) != len(keys):
        raise ValueError('S1 副本 context key/code 必须跨常规、隐藏、特殊副本全局唯一')


_validate_s1_dungeon_context_keys()

S1_DUNGEON_BY_NAME = {row['name']: row for row in S1_DUNGEONS}


def get_s1_dungeon_context_tag(target_dungeon: str | None) -> str | None:
    """按稳定身份返回副本标签；未知展示名不错误回退到首个副本。"""
    name = str(target_dungeon or '').strip()
    row = S1_DUNGEON_BY_NAME.get(name)
    key = row.get('context_key') if row else None
    if key is None:
        row = S1_HIDDEN_DUNGEON_BY_NAME.get(name)
        key = row.get('code') if row else None
    if key is None:
        row = S1_SPECIAL_DUNGEON_MAP.get(name)
        key = row.get('context_key') if row else None
    return f'dungeon:{key}' if _is_s1_dungeon_context_key(key) else None


def get_s1_dungeon_context_tag_catalog() -> dict[str, str]:
    """分类 AI 使用的展示名→稳定标签白名单；不靠模型翻译或哈希。"""
    names = [
        *(row['name'] for row in S1_DUNGEONS),
        *(row['name'] for row in S1_HIDDEN_DUNGEONS),
        *(row['name'] for row in S1_SPECIAL_DUNGEONS),
    ]
    return {
        name: tag
        for name in names
        if (tag := get_s1_dungeon_context_tag(name)) is not None
    }

# 门票制特殊副本固定 4 人组队；未满员保持 forming（集结中），满员后启动出发倒计时。
S1_SPECIAL_PARTY_TARGET_SIZE = 4


def get_s1_special_party_target_size() -> int:
    return S1_SPECIAL_PARTY_TARGET_SIZE


def get_s1_special_dungeons_by_ticket_rarity(rarity: str) -> list[dict]:
    """按门票稀有度筛选特殊副本（拾荒掉落分池用）。"""
    target = str(rarity or 'epic').strip().lower()
    return [
        d for d in S1_SPECIAL_DUNGEONS
        if str(d.get('ticket_rarity') or 'epic').strip().lower() == target
        and d.get('scavenge_drop_enabled', True)
    ]


def is_s1_special_dungeon(target_dungeon: str | None) -> bool:
    """是否为门票制特殊副本。"""
    return bool(target_dungeon) and target_dungeon in S1_SPECIAL_DUNGEON_MAP


def get_s1_special_dungeon(target_dungeon: str | None) -> dict | None:
    """取特殊副本定义；非特殊副本返回 None。"""
    if not target_dungeon:
        return None
    return S1_SPECIAL_DUNGEON_MAP.get(target_dungeon)


def get_s1_special_dungeon_ticket(target_dungeon: str | None) -> str | None:
    """取特殊副本所需门票图鉴名；非特殊副本返回 None。"""
    info = get_s1_special_dungeon(target_dungeon)
    return info['ticket_item'] if info else None


def get_s1_dungeon_info(target_dungeon: str | None) -> dict:
    """统一取副本信息（含常规与特殊）。

    常规副本返回 S1_DUNGEONS 中的原始条目；特殊副本合成一个 min_level=1 / max_level=999
    的不限等级条目（供探险 prompt 的等级文案与危险度评估使用）；未知名回退首个常规副本。
    """
    special = get_s1_special_dungeon(target_dungeon)
    if special:
        return {'name': special['name'], 'min_level': 1, 'max_level': 999, 'is_special': True}
    hidden = get_s1_hidden_dungeon(target_dungeon)
    if hidden:
        source_info = next(
            (d for d in S1_ACTIVE_DUNGEONS if d['name'] == hidden['source_dungeon']),
            S1_ACTIVE_DUNGEONS[0],
        )
        level_offset = _get_s1_hidden_dungeon_level_offset(hidden)
        return {
            'name': hidden['name'],
            'min_level': source_info['min_level'] + level_offset,
            'max_level': source_info['max_level'] + level_offset,
            'is_hidden': True,
            'source_dungeon': hidden['source_dungeon'],
        }
    for dungeon_info in S1_DUNGEONS:
        if dungeon_info['name'] == target_dungeon:
            return dungeon_info
    from .s1_campaign_world_service import campaign_world
    world = campaign_world(target_dungeon)
    if world:
        return world
    return S1_DUNGEONS[0]


def get_s1_dungeon_level_range(target_dungeon: str | None) -> tuple[int, int]:
    """根据 S1 地牢名取推荐等级区间；特殊副本/未知名回退 (1, 999)。"""
    if is_s1_special_dungeon(target_dungeon):
        return 1, 999
    hidden = get_s1_hidden_dungeon(target_dungeon)
    if hidden:
        source_info = next(
            (d for d in S1_ACTIVE_DUNGEONS if d['name'] == hidden['source_dungeon']),
            None,
        )
        if source_info:
            level_offset = _get_s1_hidden_dungeon_level_offset(hidden)
            return source_info['min_level'] + level_offset, source_info['max_level'] + level_offset
    for dungeon_info in S1_DUNGEONS:
        if dungeon_info['name'] == target_dungeon:
            return dungeon_info['min_level'], dungeon_info['max_level']
    from .s1_campaign_world_service import campaign_world
    world = campaign_world(target_dungeon)
    if world:
        return world['min_level'], world['max_level']
    return 1, 999


def format_s1_dungeon_level_desc(target_dungeon: str | None) -> str | None:
    """根据地牢名格式化为推荐等级展示文案，如 Lv.1-3 / Lv.15+；无目标副本时返回 None。

    特殊副本不按等级档位匹配，统一返回其 level_label（如「特殊·不限等级」）。
    """
    if not target_dungeon:
        return None
    special = get_s1_special_dungeon(target_dungeon)
    if special:
        return special.get('level_label') or '特殊副本'
    min_level, max_level = get_s1_dungeon_level_range(target_dungeon)
    if max_level >= 999:
        return f'Lv.{min_level}+'
    return f'Lv.{min_level}-{max_level}'


# ── 自动组队的副本阶级（rank）映射与跨度匹配工具 ──────────────────────────────
# 把当前开放副本按 min_level 归并成离散阶级：当前开放 min_level 为 1/3/5/7/9/11/13，
# 故玩家自动组队 rank 取值 0~6。Lv.15+ 终局档保留在完整 S1_DUNGEONS，但暂不参与玩家入口。
# 组队时用 rank 差控制冒险者能加入哪些档位的小队（见
# adventurer_s1_party_service.form_s1_parties），与原版 DUNGEON_RANK_MAP 同构但独立。
S1_DUNGEON_UNIQUE_TIERS = sorted({d['min_level'] for d in S1_ACTIVE_DUNGEONS})
S1_DUNGEON_RANK_MAP = {
    d['name']: S1_DUNGEON_UNIQUE_TIERS.index(d['min_level']) for d in S1_ACTIVE_DUNGEONS
}


def _known_s1_dungeon_rank(target_dungeon: str | None) -> int | None:
    if target_dungeon in S1_DUNGEON_RANK_MAP:
        return S1_DUNGEON_RANK_MAP[target_dungeon]
    from .s1_campaign_world_service import campaign_world
    world = campaign_world(target_dungeon)
    return S1_DUNGEON_UNIQUE_TIERS.index(world['min_level']) if world else None


def get_s1_dungeon_rank(target_dungeon: str | None) -> int:
    """取地牢对应的阶级 rank；未知名回退 0（最低档）。"""
    rank = _known_s1_dungeon_rank(target_dungeon)
    return rank if rank is not None else 0


def get_adventurer_real_rank_s1(adventurer) -> int:
    """计算冒险者当前最适合的副本阶级锚点：取等级所落档位中最高的 rank。"""
    natural_ranks = [
        S1_DUNGEON_UNIQUE_TIERS.index(d['min_level'])
        for d in S1_ACTIVE_DUNGEONS
        if d['min_level'] <= adventurer.level <= d['max_level']
    ]
    return max(natural_ranks) if natural_ranks else 0


# ── 普通组队/匹配档位跨度 ─────────────────────────────────────────────────────
# 主动创建/选图：不得高于自身 rank（向下不限）。
# 加入已有小队 / 随机匹配并集：向下不限，向上最多高 SPAN_MAX_HIGHER rank。
SPAN_MAX_HIGHER = 1

_ACTIVE_S1_RANKS = frozenset(S1_DUNGEON_RANK_MAP.values())


def get_s1_joinable_ranks(adventurer) -> set[int]:
    """随机匹配并集或加入小队时的允许 rank 集合（含向上 1 档）。"""
    rank = get_adventurer_real_rank_s1(adventurer)
    if adventurer.level == 1:
        return {0}
    high = rank + SPAN_MAX_HIGHER
    return {r for r in _ACTIVE_S1_RANKS if 0 <= r <= high}


def get_s1_active_selectable_ranks(adventurer) -> set[int]:
    """主动选图时的允许 rank 集合（仅自身档及以下）。"""
    rank = get_adventurer_real_rank_s1(adventurer)
    return {r for r in _ACTIVE_S1_RANKS if r <= rank}


def check_s1_join_dungeon_rank_span(adventurer, target_dungeon: str | None) -> str | None:
    """加入已有小队：向下不限，向上最多高 SPAN_MAX_HIGHER rank。"""
    if not target_dungeon:
        return None
    target_rank = _known_s1_dungeon_rank(target_dungeon)
    if target_rank is None:
        return None
    if adventurer.level == 1 and target_rank != 0:
        name = getattr(adventurer, 'name', '冒险者')
        return f'{name} 仍为 1 级，只能挑战 rank1 副本'
    adv_rank = get_adventurer_real_rank_s1(adventurer)
    if target_rank > adv_rank + SPAN_MAX_HIGHER:
        diff = target_rank - adv_rank
        return (
            f'副本档位比冒险者高 {diff} 档，超出允许范围（最多高 {SPAN_MAX_HIGHER} 档）'
        )
    return None


def check_s1_active_dungeon_select(adventurer, target_dungeon: str | None) -> str | None:
    """主动创建/选图：不得高于自身 rank。"""
    if not target_dungeon:
        return None
    target_rank = _known_s1_dungeon_rank(target_dungeon)
    if target_rank is None:
        return None
    adv_rank = get_adventurer_real_rank_s1(adventurer)
    if target_rank > adv_rank:
        name = getattr(adventurer, 'name', '冒险者')
        return f'{name} 不能主动选择高于自身档位的副本「{target_dungeon}」'
    return None


def pick_weighted_dungeon_for_adventurer_s1(adventurer, adv_real_rank: int) -> str:
    """按冒险者等级倾向挑一个适合带队新建的地牢：同档权重 100、相邻档 10。"""
    weights = []
    for dungeon in S1_ACTIVE_DUNGEONS:
        if adventurer.level < dungeon['min_level']:
            continue

        target_rank = S1_DUNGEON_RANK_MAP.get(dungeon['name'], 0)
        rank_diff = abs(target_rank - adv_real_rank)
        if rank_diff > 1:
            continue

        weight = 100 if rank_diff == 0 else 10
        weights.append((dungeon['name'], weight))

    if not weights:
        return S1_ACTIVE_DUNGEONS[0]['name']

    total_weight = sum(weight for _, weight in weights)
    roll = random.uniform(0, total_weight)
    cumulative = 0
    for dungeon_name, weight in weights:
        cumulative += weight
        if roll <= cumulative:
            return dungeon_name
    return weights[-1][0]


def find_same_rank_forming_party_s1(forming_parties, party_member_counts, target_rank: int):
    """在同副本阶级且未满员的集结中小队里随机挑一个并入；没有则返回 None。"""
    same_rank_parties = [
        party
        for party in forming_parties
        if party_member_counts.get(party.id, 0) < party.target_size
        and S1_DUNGEON_RANK_MAP.get(party.target_dungeon, 0) == target_rank
    ]
    if not same_rank_parties:
        return None
    return random.choice(same_rank_parties)
