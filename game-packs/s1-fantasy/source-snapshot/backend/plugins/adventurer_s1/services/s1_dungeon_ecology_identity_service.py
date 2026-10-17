"""S1 副本生态身份画像与确定性轮换。

本模块只维护抽象的生态血统，不定义固定掉落、具体剧情或探索目标。选择器是
无数据库依赖的纯函数；调用方负责提供按时间倒序排列的近期结构化快照，并在
生成 AI 之前冻结返回值。
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
from itertools import product
from typing import Iterable, Mapping

from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    get_s1_hidden_dungeon,
    get_s1_hidden_dungeon_by_code,
)


ECOLOGY_IDENTITY_OUTLINE_FIELD = 'ecology_identity'
ECOLOGY_IDENTITY_SNAPSHOT_LOG_FIELD = '_ecology_identity_snapshot'
ECOLOGY_IDENTITY_REVIEW_LOG_FIELD = '_ecology_identity_review'

ECOLOGY_IDENTITY_SCHEMA_VERSION = 1
ECOLOGY_IDENTITY_PROFILE_REVISION = 1

_COMPONENT_FIELDS = (
    'driver',
    'resource_lineage',
    'evolution_mode',
    'interaction_mode',
)


def _component_entries(profile_id: str, component: str, rows: tuple[tuple[str, str], ...]) -> tuple[dict, ...]:
    return tuple({
        'ref': f'{profile_id}.{component}.{slug}',
        'description': description,
    } for slug, description in rows)


def _profile(
    profile_id: str,
    identity_anchor: str,
    *,
    drivers: tuple[tuple[str, str], ...],
    resources: tuple[tuple[str, str], ...],
    evolutions: tuple[tuple[str, str], ...],
    interactions: tuple[tuple[str, str], ...],
) -> dict:
    if not all(len(rows) == 3 for rows in (drivers, resources, evolutions, interactions)):
        raise ValueError(f'生态画像 {profile_id} 的四类维度都必须恰好包含 3 项')
    return {
        'profile_id': profile_id,
        'profile_revision': ECOLOGY_IDENTITY_PROFILE_REVISION,
        'identity_anchor': identity_anchor,
        'drivers': _component_entries(profile_id, 'driver', drivers),
        'resource_lineages': _component_entries(profile_id, 'resource', resources),
        'evolution_modes': _component_entries(profile_id, 'evolution', evolutions),
        'interaction_modes': _component_entries(profile_id, 'interaction', interactions),
    }


# 画像描述刻意停留在生态结构与资源血统层；它们不是成品剧情、目标或掉落表。
DUNGEON_ECOLOGY_PROFILES = {
    '哥布林矿坑': _profile(
        'goblin_mine',
        '哥布林氏族的采掘劳动、洞穴生境与地下交换共同塑造这座矿坑。',
        drivers=(
            ('clan_labor', '哥布林氏族按领地、工种与地位组织采掘、搬运和守矿。'),
            ('cave_foodweb', '菌床、穴居小兽与矿坑废料构成可迁移的洞穴食物网。'),
            ('salvage_trade', '废弃坑道、偷采者和地表商路持续改变可回收物的流向。'),
        ),
        resources=(
            ('worked_ore', '经哥布林筛选、敲打或粗炼后留下的矿石与冶炼副产物谱系。'),
            ('cave_crops', '依附木架、潮气与废料生长的菌类、根茎和培养介质谱系。'),
            ('salvaged_tools', '来自塌坑、旧工棚与争夺现场的工具、构件和可再加工残料谱系。'),
        ),
        evolutions=(
            ('claim_succession', '矿脉枯竭或失守后，不同氏族会重新划界、换班或迁入。'),
            ('collapse_reopening', '坍塌、排水与支护会关闭旧层并逐步打开新的矿层。'),
            ('trade_shift', '收购价、走私路线与地表压力会改变采掘重点和库存形态。'),
        ),
        interactions=(
            ('work_zones', '通过矿层、工棚、菌床与废料场的分区理解资源来源。'),
            ('clan_bargain', '可与矿主、苦工、偷采者或守矿者围绕通行和分配交涉。'),
            ('support_network', '支架、矿车、排水沟和通风孔决定探索路径与风险。'),
        ),
    ),
    '野狼林道': _profile(
        'wolf_road',
        '狼群领地、猎物迁徙与林道植被演替维持这片野性走廊。',
        drivers=(
            ('pack_territory', '狼群按气味边界、育幼地与狩猎路线周期性调整领地。'),
            ('prey_migration', '鹿兔等猎物的迁移、疾病与繁殖改变捕食压力。'),
            ('forest_succession', '风倒木、灌丛恢复和林缘扩张不断改写通行与栖息地。'),
        ),
        resources=(
            ('shed_biomass', '狼群与猎物自然脱落、换毛和繁殖留下的生物材料谱系。'),
            ('predation_remains', '捕食、啃食与腐食过程留下的骨、皮及分解副产物谱系。'),
            ('forest_growth', '受踩踏、粪肥和林隙光照影响的药草、木材与种实谱系。'),
        ),
        evolutions=(
            ('pack_turnover', '头狼更替、分群或育幼会改变核心领地与警戒方式。'),
            ('prey_wave', '猎物丰歉会推动狼群扩张、迁徙或暂时离开林道。'),
            ('regrowth_cycle', '火烧、风倒或采伐后的植被会按先锋到成熟群落演替。'),
        ),
        interactions=(
            ('scent_boundaries', '气味标记、足迹和嚎叫构成可读的领地信号。'),
            ('trail_layers', '兽径、旧商道与林冠缺口形成不同的移动层。'),
            ('predator_balance', '驱赶、绕行或改变猎物分布都会影响狼群后续行为。'),
        ),
    ),
    '荒野哨站': _profile(
        'frontier_outpost',
        '边地补给、巡防秩序与占领者更替决定哨站的物资和生境。',
        drivers=(
            ('frontier_logistics', '补给车队、储水与轮值制度维持哨站基本运转。'),
            ('patrol_pressure', '巡逻范围、烽火联络与道路威胁改变人员和物资分布。'),
            ('occupier_turnover', '守军、盗匪、难民或佣兵会先后占用同一套设施。'),
        ),
        resources=(
            ('stored_supplies', '仓储、口粮、药品和燃料在搬运、腐坏与补充中形成的谱系。'),
            ('fort_salvage', '栅墙、烽火台、兵器架与废车中可维修再用的构件谱系。'),
            ('frontier_craft', '驻守者就地制作的皮具、绳索、箭材和简易器械谱系。'),
        ),
        evolutions=(
            ('occupation_cycle', '失守与重占会改变旗号、制度和设施用途而不抹去哨站。'),
            ('supply_cycle', '补给中断后库存会耗尽，路线恢复又带来不同批次物资。'),
            ('decay_repair', '荒废设施逐渐损坏，新的占领者会选择性修复或改造。'),
        ),
        interactions=(
            ('defense_zones', '岗楼、营房、仓库与外围工事对应不同权限和资源。'),
            ('signal_network', '烽火、旗语、路标和巡逻记录可改变接近方式。'),
            ('occupier_relations', '当前占领群体的纪律、诉求和敌友关系决定交涉空间。'),
        ),
    ),
    '迷雾沼泽': _profile(
        'mist_marsh',
        '水位循环、湿地食物网与毒性共生构成迷雾沼泽的持续生态。',
        drivers=(
            ('water_cycle', '降雨、潮汐和地下水让可通行地与浸水区周期交换。'),
            ('toxic_symbiosis', '菌类、毒植、虫群和两栖生物以毒素交换形成共生链。'),
            ('wetland_foodweb', '腐屑、鱼虫、涉禽与捕食者随季节改变密度和位置。'),
        ),
        resources=(
            ('spore_plant', '由湿气、腐泥和共生菌滋养的孢子、药植与纤维谱系。'),
            ('venom_secretion', '毒虫、两栖生物与毒植分泌、蜕落和稀释形成的谱系。'),
            ('silt_deposit', '水流沉积、腐殖分解与矿物渗滤共同形成的泥炭和结核谱系。'),
        ),
        evolutions=(
            ('flood_recession', '涨水淹没旧采集点，退水后在别处暴露沉积与巢区。'),
            ('decomposition_bloom', '枯败物分解会先消耗环境，随后催生菌群和新植被。'),
            ('recolonization', '局部受扰后，虫群、浮岛植物和捕食者会按食物链重新占领。'),
        ),
        interactions=(
            ('water_routes', '浅滩、水道、浮岛和树根层决定到达方式与时间窗口。'),
            ('toxicity_gradients', '颜色、气味和生物反应揭示毒性浓度而非通用能量读数。'),
            ('foodweb_leverage', '改变一种生物的数量会沿食物链影响后续资源和危险。'),
        ),
    ),
    '叹息地牢': _profile(
        'sighing_dungeon',
        '监禁制度、看守设施与囚徒记忆的继承让叹息地牢持续演变。',
        drivers=(
            ('custodial_system', '牢门、巡查、禁制和配给曾共同维持分层监禁秩序。'),
            ('inmate_warden_succession', '看守衰亡后，囚徒后裔、拾荒者或新守卫接管不同牢区。'),
            ('echo_memory', '长期痛苦、口述暗号和残留仪式让记忆依附于具体牢室与通道。'),
        ),
        resources=(
            ('restraint_craft', '刑具、锁具、栏栅和抑制设施磨损后留下的材料与构件谱系。'),
            ('prison_records', '名册、刻痕、密语与配给凭证承载的记录和知识谱系。'),
            ('ritual_residue', '审讯、忏悔或封禁仪式在牢区介质中留下的残余谱系。'),
        ),
        evolutions=(
            ('seal_succession', '旧封禁衰退后会被修补、绕过或替换为新的控制方式。'),
            ('reoccupation', '空牢区会被逃亡者、怪物或新管理者改造成不同用途。'),
            ('memory_reframing', '新证词和发现会改变牢区传说，但不会抹去可核对的旧痕迹。'),
        ),
        interactions=(
            ('cellblock_hierarchy', '牢区、看守道、审讯室和后勤层构成权限清晰的空间。'),
            ('lock_and_route', '钥匙、机关、暗号和隐藏通道决定转移路线。'),
            ('testimony_network', '幸存者、记录与回声可互相验证，形成区别于采矿的探索方式。'),
        ),
    ),
    '废弃修道院': _profile(
        'abandoned_monastery',
        '礼仪遗存、圣物看护与药草庭院共同延续废弃修道院的生命。',
        drivers=(
            ('ritual_calendar', '祷告、葬仪和节期留下的空间次序仍影响遗迹变化。'),
            ('reliquary_care', '残存教团、守墓者或自律构造物持续维护部分圣物与封闭区。'),
            ('cloister_garden', '药圃、蜂群、井水和墓园植物形成独立于礼拜堂的生境。'),
        ),
        resources=(
            ('ritual_material', '香料、灯油、经页、祭器维护物与仪式耗材的谱系。'),
            ('relic_conservation', '圣物包覆、墓窟防腐和修复作业产生的材料谱系。'),
            ('medicinal_garden', '药草、蜂蜡、种子与受墓园土壤影响的园艺产物谱系。'),
        ),
        evolutions=(
            ('faith_succession', '旧教团消失后，新信众、异端或守护者会重释并占用礼仪空间。'),
            ('caretaker_cycle', '维护者增减决定哪些区域被保存、封锁或重新开放。'),
            ('garden_rewilding', '无人照料的药圃会野化，并在修复后产生不同的可用群落。'),
        ),
        interactions=(
            ('sacred_zoning', '礼拜堂、回廊、墓窟与药圃具有不同禁忌和资源来源。'),
            ('liturgy_access', '钟声、经序、誓言和仪式动作可影响通路与看护者态度。'),
            ('care_or_plunder', '修复、交换或掠夺会给遗迹留下不同的长期后果。'),
        ),
    ),
    '剧毒钟乳洞': _profile(
        'venom_stalactite_cave',
        '毒性渗流、穴居繁殖与钟乳矿化共同塑造剧毒钟乳洞。',
        drivers=(
            ('mineral_seep', '地下水携带矿物和毒性物质，在不同洞层缓慢渗积。'),
            ('brood_cycle', '蛇类、节肢生物和两栖种群围绕产卵与蜕变迁移。'),
            ('fungal_filter', '洞穴菌毯吸附、转化毒素并为小型生物提供食物。'),
        ),
        resources=(
            ('molt_venom', '穴居生物的蜕皮、毒腺分泌和卵膜构成的生物谱系。'),
            ('toxic_mineral', '毒性水滴与钟乳矿层反应形成的沉积和结晶谱系。'),
            ('cave_fungi', '耐毒菌丝、孢囊和吸附介质构成的真菌谱系。'),
        ),
        evolutions=(
            ('brood_migration', '繁殖期结束后巢区会迁移，旧巢逐步被腐食者利用。'),
            ('groundwater_shift', '渗流改道会耗尽旧沉积点并在新裂隙形成矿化带。'),
            ('predator_rebalance', '捕食者减少或激增会改变毒物种和滤毒菌群的比例。'),
        ),
        interactions=(
            ('vertical_caverns', '洞顶、石柱、地下水面和深坑形成垂直生态层。'),
            ('toxicity_signs', '生物行为与矿膜变化提供毒性证据，而非统一脉冲提示。'),
            ('brood_boundaries', '育幼区、觅食区和蜕皮场的边界决定风险与可采集性。'),
        ),
    ),
    '狂沙荒漠': _profile(
        'sand_wastes',
        '风沙地貌、绿洲水脉与耐旱生物迁移驱动狂沙荒漠。',
        drivers=(
            ('dune_weather', '季风和沙暴不断搬运沙丘、暴露岩层并掩埋旧道路。'),
            ('oasis_cycle', '地下水位、盐化与植被蒸腾决定绿洲的兴衰。'),
            ('burrower_caravan', '穴居生物活动与商旅路线共同改变补给和遗留物分布。'),
        ),
        resources=(
            ('wind_exposed_mineral', '风蚀暴露的矿物、玻璃质和盐类沉积谱系。'),
            ('arid_biological', '耐旱植株、甲壳、毒液与储水组织构成的生物谱系。'),
            ('caravan_remnant', '商旅修理、弃置和交换留下的织物、金属与补给残料谱系。'),
        ),
        evolutions=(
            ('dune_migration', '沙丘移动会掩埋旧点位并暴露新的岩架、遗迹或巢穴。'),
            ('oasis_turnover', '水源衰退后生境收缩，水脉回升时植物和动物重新扩张。'),
            ('colony_shift', '穴居群落随温度、食物和捕食压力迁往不同沙层。'),
        ),
        interactions=(
            ('weather_windows', '风向、温差和能见度构成真实的旅行窗口。'),
            ('water_claims', '绿洲使用权和商旅补给关系可通过交涉或守护改变。'),
            ('surface_subsurface', '地表路线、埋藏层和穴居通道提供差异明显的探索面。'),
        ),
    ),
    '风暴尖塔': _profile(
        'storm_spire',
        '高空气候、导雷结构与飞行生物群落维持风暴尖塔。',
        drivers=(
            ('atmospheric_charge', '云层、电荷和强风在塔体不同高度周期聚散。'),
            ('avian_elemental', '飞行兽群与风雷元素在巢位、上升气流和猎场间竞争。'),
            ('weather_machinery', '古老导体、叶轮与观测设施仍会调节局部风暴。'),
        ),
        resources=(
            ('conductor_material', '导雷构件受电蚀、氧化和维修形成的金属与绝缘材料谱系。'),
            ('charged_organic', '飞行生物羽膜、巢材和受电环境影响的分泌物谱系。'),
            ('instrument_salvage', '气象仪、传动件和校准介质留下的器械材料谱系。'),
        ),
        evolutions=(
            ('charge_cycle', '放电耗散后旧节点沉寂，新的高势区随天气重新形成。'),
            ('nest_succession', '巢群迁飞、繁殖或被捕食后，不同物种会接替高塔层位。'),
            ('machinery_reconfiguration', '损坏、修复或误触会改变导流结构和可进入路线。'),
        ),
        interactions=(
            ('altitude_layers', '塔基、云下层、导雷层和塔冠具有不同气候与生物。'),
            ('conductor_network', '理解接地、绝缘和导流关系比追逐通用能量更重要。'),
            ('flight_corridors', '上升气流、巢区与风障决定生物和冒险者的移动。'),
        ),
    ),
    '暗影迷宫': _profile(
        'shadow_labyrinth',
        '移动建筑、明暗交换与依附记忆的影生物构成暗影迷宫。',
        drivers=(
            ('moving_architecture', '墙体、门廊与空间连接会按迷宫自身规则重新排列。'),
            ('shadow_foodweb', '影生物以光差、恐惧和残留记忆为生并彼此捕食。'),
            ('lost_memory', '来访者遗落的路线认知和物件会成为迷宫的一部分。'),
        ),
        resources=(
            ('shade_byproduct', '影生物蜕散、凝聚和捕食留下的暗质生物材料谱系。'),
            ('maze_material', '会移位的墙材、门轴、镜面与标记介质谱系。'),
            ('lost_artifact', '历代来访者遗失且被空间重新分配的器物与记录谱系。'),
        ),
        evolutions=(
            ('layout_shift', '路径被确认或破坏后，迷宫会重排连接而非恢复同一房间。'),
            ('shadow_succession', '光源与猎物变化会让不同影生物接替占据区域。'),
            ('occupancy_imprint', '新来访者的行动会留下记忆痕迹，改变后续诱导和禁区。'),
        ),
        interactions=(
            ('light_geometry', '光源角度、遮蔽物与反射关系决定可见路径。'),
            ('memory_mapping', '路线记录必须与现场核对，错误记忆本身可能成为地形。'),
            ('moving_zones', '房间功能和邻接关系变化要求追踪结构而非固定坐标。'),
        ),
    ),
    '冰霜峡谷': _profile(
        'frost_gorge',
        '冻融地貌、寒地兽群与冰川运动塑造冰霜峡谷。',
        drivers=(
            ('freeze_thaw', '昼夜与季节温差使冰层反复开裂、封冻和挤压。'),
            ('cold_fauna', '寒地兽群沿风障、猎物和育幼地迁徙。'),
            ('glacier_motion', '冰川缓慢移动并搬运岩屑、遗骸与旧设施。'),
        ),
        resources=(
            ('icebound_organic', '被冰封保存的植物、兽类组织和古老有机物谱系。'),
            ('frost_mineral', '冻融浓缩、冰川磨蚀和盐析产生的矿物谱系。'),
            ('cold_fauna_shed', '寒地生物换毛、蜕角、筑巢与捕食留下的材料谱系。'),
        ),
        evolutions=(
            ('glacier_advance_retreat', '冰川进退会封住旧层并在融水带暴露新沉积。'),
            ('crevasse_cycle', '裂隙开启、雪桥形成与崩塌持续改变路线。'),
            ('herd_migration', '兽群随食物和风暴迁移，留下的资源和捕食者也随之移动。'),
        ),
        interactions=(
            ('thermal_layers', '向阳坡、风口、冰下水道与洞穴具有不同温度生态。'),
            ('ice_reading', '裂纹、气泡和沉积层可用于判断冰体历史与承载风险。'),
            ('herd_routes', '迁徙道、育幼地和捕食伏击区构成动态生物地图。'),
        ),
    ),
    '食人魔要塞': _profile(
        'ogre_fortress',
        '食人魔氏族、饲养与酿造体系以及攻防工事维持要塞生态。',
        drivers=(
            ('clan_hierarchy', '氏族首领、猎手、厨役和工匠按力量与血缘分配空间。'),
            ('larder_livestock', '猎物、牲畜、储藏和腐食者构成粗放但持续的食物系统。'),
            ('siege_craft', '锻炉、投石器与墙体维修消耗并再生大量材料流。'),
        ),
        resources=(
            ('biological_stock', '饲养、狩猎和屠宰产生的骨、皮、脂与饲料副产物谱系。'),
            ('fermentation', '酒桶、盐渍、发酵物与微生物培养介质构成的谱系。'),
            ('forge_siege', '粗锻金属、石材、绳索和攻城器械磨损件谱系。'),
        ),
        evolutions=(
            ('clan_takeover', '氏族更替会重分仓库、工坊和居住层，而要塞本体延续。'),
            ('larder_cycle', '盛宴耗空存货后，狩猎、饲养和交换逐步补入不同资源。'),
            ('fort_repair', '攻防损伤会促使新墙、陷阱和废料场在不同位置出现。'),
        ),
        interactions=(
            ('clan_zones', '首领厅、兽栏、厨房、锻炉与城墙对应不同群体。'),
            ('tribute_bargain', '贡品、挑战、交易或挑拨可改变氏族关系和通行。'),
            ('supply_routes', '猎道、后门、运料坡和排污渠体现要塞的真实代谢。'),
        ),
    ),
    '遗忘废墟': _profile(
        'forgotten_ruins',
        '失落城市设施、自律构造物与拾荒聚落共同改写遗忘废墟。',
        drivers=(
            ('automata_maintenance', '残存构造物按旧职责巡检、修复或拆解设施。'),
            ('memory_decay', '档案、碑刻和导航系统逐步损坏并产生互相矛盾的城市记忆。'),
            ('scavenger_settlement', '拾荒者、流民与怪物在可供水和可防守的街区定居。'),
        ),
        resources=(
            ('inscription_archive', '碑文、存储介质、地图和行政记录构成的知识谱系。'),
            ('mechanism_parts', '构造物、门机、管网和能源设施拆换产生的部件谱系。'),
            ('urban_salvage', '建筑材料、生活器具和聚落再加工品构成的城市残料谱系。'),
        ),
        evolutions=(
            ('district_reopening', '清障、供能或坍塌会让不同街区依次关闭和重新开放。'),
            ('memory_reconstruction', '新发现可校正城市历史，但破损档案不会凭空复原。'),
            ('settlement_succession', '聚落兴衰和构造物巡逻会改变废墟的实际控制区。'),
        ),
        interactions=(
            ('infrastructure_graph', '道路、管网、门禁与供能关系决定可达性。'),
            ('record_crosscheck', '碑刻、机械日志和居民口述需要相互核验。'),
            ('district_authority', '构造物规则与现住群体权利共同决定资源使用方式。'),
        ),
    ),
    '熔岩火山': _profile(
        'lava_volcano',
        '岩浆脉动、火生物群落与冷却后的火山地貌驱动熔岩火山。',
        drivers=(
            ('magma_cycle', '岩浆上涌、退却与气体释放改变热区和矿物分布。'),
            ('fire_organisms', '耐热生物、火元素和灰烬植物围绕温度梯度繁衍。'),
            ('ash_weather', '喷发灰、酸气与降水重塑坡面土壤和能见度。'),
        ),
        resources=(
            ('volcanic_glass_metal', '熔融、分异和急冷产生的玻璃质、金属与矿渣谱系。'),
            ('fire_biological', '耐热生物的甲壳、分泌物、卵与共生菌谱系。'),
            ('ash_gas', '火山灰、硫盐、凝结物与气孔沉积形成的谱系。'),
        ),
        evolutions=(
            ('eruption_cooling', '喷发耗散后熔道冷却，旧热源封闭并在别处重开。'),
            ('lava_tube_colonization', '冷却熔洞会依次被微生物、猎物与大型生物占据。'),
            ('ash_soil_succession', '灰地从贫瘠沉积逐步演化为耐热植被群落。'),
        ),
        interactions=(
            ('heat_gradients', '熔口、冷壳、气孔和灰坡构成不同热力层。'),
            ('gas_vent_network', '风向与气孔连通决定毒气、蒸汽和可达路线。'),
            ('cooling_windows', '冷却速度和壳层稳定性提供有限但可推断的行动窗口。'),
        ),
    ),
    '古王陵寝': _profile(
        'ancient_royal_tomb',
        '王朝葬制、祖灵秩序与盗掘修复的拉扯维持古王陵寝。',
        drivers=(
            ('funerary_institution', '墓室等级、随葬职责与祭祀路线体现王朝制度。'),
            ('ancestral_spirits', '祖灵、守墓兽与后裔祭仪维持不同层级的禁忌。'),
            ('looter_vermin', '盗墓者、穴兽和腐食者利用破损入口形成次生生态。'),
        ),
        resources=(
            ('ritual_conservation', '防腐、祭祀、封缄和修复材料构成的谱系。'),
            ('spirit_byproduct', '祖灵显现、祭品交换与镇抚过程留下的介质谱系。'),
            ('tomb_craft', '石刻、机关、棺椁与陪葬工艺的构件和原料谱系。'),
        ),
        evolutions=(
            ('seal_breach_restore', '封层被盗掘或破坏后，守墓力量会修补、转移或增设禁制。'),
            ('ancestral_cycle', '祭期、王朝记忆和后裔活动改变祖灵的活跃区域。'),
            ('flood_sediment', '地下水与沉积会封住旧室并在排水后暴露新的墓道。'),
        ),
        interactions=(
            ('funerary_hierarchy', '祭道、陪葬室、主墓与守墓区体现身份秩序。'),
            ('rite_and_taboo', '遵循或破坏葬仪会改变祖灵和守护者的响应。'),
            ('breach_trace', '盗洞、修补痕迹和沉积层可用于追踪真实进入历史。'),
        ),
    ),
    '巨龙巢穴': _profile(
        'dragon_nest',
        '活跃巨龙及幼龙、巢域等级和猎物腐食链共同构成巨龙巢穴。',
        drivers=(
            ('dragon_brood', '成年龙、幼龙与卵围绕育幼、蜕生和照护形成长期活动。'),
            ('territorial_hierarchy', '龙族威压、气味和领空划分决定巢层与附庸的地位。'),
            ('prey_scavenger_web', '猎物迁入、捕食残留与耐龙威腐食者维持巢域食物网。'),
        ),
        resources=(
            ('dragon_shed', '龙类自然脱落的鳞屑、角质、巢液与生理副产物谱系。'),
            ('nest_byproduct', '巢材、育幼介质、呼吸影响物与长期栖居沉积谱系。'),
            ('prey_hoard_weathering', '猎物残留、贡物和藏品经啃食、腐化或环境作用形成的谱系。'),
        ),
        evolutions=(
            ('brood_succession', '幼龙成长、分巢或新卵诞生会改变各巢层用途。'),
            ('territory_migration', '巨龙迁徙、归巢或领主更替会重划领空和狩猎范围。'),
            ('scavenger_recolonization', '龙族暂离或猎物变化后，附庸与腐食者会分阶段重新占领。'),
        ),
        interactions=(
            ('territory_signals', '爪痕、气味、吼声与飞行轨迹构成明确的龙域信号。'),
            ('vertical_nest_layers', '巢底猎物区、育幼层、起飞台和高位主巢形成垂直结构。'),
            ('dragon_social_order', '巨龙、幼龙、附庸和偷巢者的关系决定交涉与冲突。'),
        ),
    ),
    '恶魔岛': _profile(
        'demon_island',
        '恶魔族群、契约仪式、派系争夺与腐化生境共同维持恶魔岛。',
        drivers=(
            ('demon_factions', '不同恶魔派系按真名、位阶和领地组织争夺与统治。'),
            ('infernal_bargains', '契约、贡赋和债务让力量与资源沿社会关系流动。'),
            ('corrupted_biota', '岛上动植物受恶魔气息长期改造，形成可繁殖的腐化生态。'),
        ),
        resources=(
            ('demonic_biological', '恶魔蜕生、伤愈、繁殖与分泌产生的生物材料谱系。'),
            ('corruption_transformed', '本地岩土、植被和生物被不同派系腐化后形成的物质谱系。'),
            ('contract_ritual', '契约介质、献祭耗材、真名封存物与派系工艺副产物谱系。'),
        ),
        evolutions=(
            ('faction_occupation', '派系胜负会改变据点、劳役和资源用途，而非让恶魔身份消失。'),
            ('pact_economy', '契约履行、违约与债务转移会持续重组岛上交换网络。'),
            ('corruption_recolonization', '净化或耗竭后的区域会被残余腐化、本地生物或新恶魔重新占领。'),
        ),
        interactions=(
            ('ranked_territories', '领主据点、附庸区、猎场与流放地体现恶魔位阶。'),
            ('bargain_network', '真名、承诺、债务和见证者构成可追踪的契约关系。'),
            ('corruption_gradients', '不同派系和生物造成的腐化具有来源明确的形态差异。'),
        ),
    ),
    '幽冥王都': _profile(
        'nether_capital',
        '亡者市民、祖灵官僚与魂流基础设施维持幽冥王都。',
        drivers=(
            ('undead_civic_order', '亡者居民按街区、行业和生前身份维持城市生活。'),
            ('ancestral_bureaucracy', '谱牒、冥契和官署程序分配记忆、通行与祭祀。'),
            ('soul_currents', '魂流沿城门、河渠和祭坛移动，为不同区域提供活性。'),
        ),
        resources=(
            ('ghost_market', '亡者交换的记忆寄物、祭品转化物与幽冥手工品谱系。'),
            ('memory_record', '谱牒、案卷、墓志和灵魂印记构成的记录谱系。'),
            ('funerary_craft', '纸扎、骨木、冥灯与魂流设施维护材料谱系。'),
        ),
        evolutions=(
            ('dynastic_succession', '王朝祭祀兴衰会改变官署合法性和街区归属。'),
            ('bureaucratic_reform', '积案、失名和新亡者涌入会促使制度重编。'),
            ('soul_tide', '魂流涨落会开启旧城区、淹没通道或改变居民活跃度。'),
        ),
        interactions=(
            ('civic_districts', '市集、官署、宗庙和居民区具有不同规则和资源。'),
            ('name_and_record', '身份、谱牒和合法凭证比单纯战斗更能改变通行。'),
            ('soul_waterways', '魂河、冥灯和祭坛节点构成城市的流动网络。'),
        ),
    ),
    '黑日王庭': _profile(
        'black_sun_court',
        '日蚀王权、宫廷派系与昏光生物群落构成黑日王庭。',
        drivers=(
            ('eclipse_cycle', '黑日明暗变化影响仪典、植物活性和阴影生物出没。'),
            ('court_factions', '王族、祭司、侍从与反叛派系围绕恩宠和领地竞争。'),
            ('ceremonial_industry', '贡赋、服饰、宴仪和武备工坊持续消耗并再造资源。'),
        ),
        resources=(
            ('regalia_craft', '冠冕、织物、漆饰和仪仗维护产生的宫廷工艺谱系。'),
            ('eclipsed_biota', '昏光花木、影兽与日蚀共生物产生的生物材料谱系。'),
            ('tribute_stock', '各领地贡品经储藏、分封和再加工形成的多来源谱系。'),
        ),
        evolutions=(
            ('court_succession', '恩宠、政变与继承会重分宫殿功能和资源控制。'),
            ('eclipse_phase', '不同蚀相让生物、仪器和仪式空间轮流活跃。'),
            ('domain_realignment', '属地叛服与贡路变化会改变王庭的物资组成。'),
        ),
        interactions=(
            ('court_protocol', '觐见次序、服色与礼仪决定社会空间的可达性。'),
            ('light_shadow_axis', '日光、蚀影与建筑朝向形成可读的生态分层。'),
            ('faction_patronage', '选择盟友、交换恩惠或揭露把柄会改变后续控制。'),
        ),
    ),
    '陨落星都': _profile(
        'fallen_star_city',
        '星界城市设施、重力异变与幸存者拾荒社会塑造陨落星都。',
        drivers=(
            ('stellar_infrastructure', '星轨供能、交通与居住系统在坠落后仍局部运作。'),
            ('gravity_anomaly', '破碎城区的重力方向和潮汐随星体残片位置变化。'),
            ('survivor_scavengers', '幸存社群、异星生物与拾荒者争夺可居住街区。'),
        ),
        resources=(
            ('urban_stellar_tech', '供能、导航、生活设施和修复作业产生的技术材料谱系。'),
            ('starfall_debris', '撞击、烧蚀和长期风化形成的星体碎屑与熔融物谱系。'),
            ('resident_craft', '幸存者适应异重力制作的工具、织物和循环物资谱系。'),
        ),
        evolutions=(
            ('district_reactivation', '供能恢复或故障会让不同城区轮流可居住、可进入。'),
            ('impact_weathering', '撞击层在风化与清理后暴露不同年代的材料。'),
            ('gravity_realignment', '星体残片移动会改写上下方向、坠落区和迁徙路线。'),
        ),
        interactions=(
            ('urban_systems', '供能、交通、空气与门禁系统相互依赖。'),
            ('gravity_layers', '不同朝向的街区和漂浮碎片构成立体探索面。'),
            ('survivor_compacts', '居民协定、修复义务和拾荒边界决定资源使用。'),
        ),
    ),
    '虚空神殿': _profile(
        'void_temple',
        '虚空礼仪、守护阶序与适应虚无的生物群落维持虚空神殿。',
        drivers=(
            ('void_liturgy', '祭仪以次序、观测和自我约束维持现实边界。'),
            ('guardian_order', '祭司遗存、守护构造物与受誓者分层看守禁区。'),
            ('void_adapted_ecology', '依附空间裂隙、低能环境和感知残响的生物持续繁衍。'),
        ),
        resources=(
            ('ritual_medium', '封界、观测、供奉与净化使用过的仪式材料谱系。'),
            ('void_biological', '适应虚无生物的膜、壳、孢体与代谢副产物谱系。'),
            ('knowledge_votive', '戒律记录、观测数据、誓物和记忆寄托物构成的谱系。'),
        ),
        evolutions=(
            ('ritual_cycle', '仪式完成、失传或重启会轮换神殿区域的用途与危险。'),
            ('incursion_reseal', '虚空侵入后守护者会封闭旧裂口，并在新的薄弱处设防。'),
            ('congregation_succession', '旧祭团衰落后，受誓者、异端或虚空生物接替空间。'),
        ),
        interactions=(
            ('liturgy_sequence', '动作、次序和禁忌决定能否安全通过仪式空间。'),
            ('perception_layers', '可见、可听、可记忆的区域并不完全重合。'),
            ('custody_relations', '守护者的职责、誓言与封印权限形成可交涉的秩序。'),
        ),
    ),
}


# 隐藏副本必须以稳定 code 拥有独立权威画像。下列条目只描述场所机制、资源
# 血统与演替方式；即使措辞参考了固定掉落的来源，也不构成掉落承诺或成品剧情。
HIDDEN_DUNGEON_ECOLOGY_PROFILES = {
    'black_iron_private_mine': _profile(
        'hidden.black_iron_private_mine',
        '黑铁私矿是哥布林矿坑之下由私矿契约、黑铁矿床与秘密冶炼共同维持的封闭采掘层。',
        drivers=(
            ('private_claims', '矿主、包工氏族与偷采者以契据和暗记争夺未公开矿层。'),
            ('black_iron_geology', '富铁围岩、还原性地下水与高压裂隙控制黑铁矿化。'),
            ('covert_smelting', '隐蔽炉窑、通风井与走私路线持续搬运矿料和燃料。'),
        ),
        resources=(
            ('iron_host_rock', '黑铁矿化带、伴生矿物和选矿尾料形成的地质材料谱系。'),
            ('furnace_accretion', '秘密冶炼产生的炉瘤、矿渣、耐火衬料与烟尘沉积谱系。'),
            ('mine_support_stock', '支护木、矿灯介质、轨车构件与修补零件构成的生产物资谱系。'),
        ),
        evolutions=(
            ('claim_turnover', '矿层收益变化会使契约转手、氏族夺权或偷采区转为公开工区。'),
            ('redox_shift', '排水和通风改变氧化还原环境，使旧矿面贫化并在裂隙暴露新矿相。'),
            ('tailing_rework', '尾矿被反复筛选、坍塌掩埋或由耐金属菌群重新富集。'),
        ),
        interactions=(
            ('claim_ledgers', '契据、产量账和矿壁暗记可用来识别权属与偷采边界。'),
            ('ore_heat_layers', '原生矿层、选矿台和炉窑区必须按地质与热工关系分别判断。'),
            ('labor_bargains', '矿主、炉工、搬运者与走私者围绕通风、配额和出路形成交涉。'),
        ),
    ),
    'subterranean_azure_tide': _profile(
        'hidden.subterranean_azure_tide',
        '地心蓝潮海是哥布林矿坑断层深处由地下含水层潮汐、矿物喷口与盲生水族共同维持的洞海。',
        drivers=(
            ('aquifer_tide_pulse', '多层含水带按压力差周期灌入洞海，让水线、盐度与可达岩台持续移动。'),
            ('mineral_vent_fields', '裂隙喷口带出不同温度与矿盐，塑造彼此分隔的化能微环境。'),
            ('blind_aquatic_communities', '失去视力的鱼群、甲壳群与附岩生物沿水流和振动重组栖位。'),
        ),
        resources=(
            ('vent_surface_accumulation', '喷口周围的矿盐沉积、微生物被膜与多孔岩壳形成的界面材料谱系。'),
            ('cave_aquatic_shed', '盲生水族换落的鳞甲、外壳、卵囊与巢材形成的生物材料谱系。'),
            ('submerged_cave_growth', '暗水藻毯、根状菌群和浸水苔衣形成的洞海生长物谱系。'),
        ),
        evolutions=(
            ('tide_chamber_succession', '含水层涨落会淹没旧栖地，并在新露出的洞室边缘启动另一轮定殖。'),
            ('vent_field_relocation', '裂隙封闭或重开会让旧喷口休眠，并把矿化和化能活动迁向别处。'),
            ('shoal_nesting_migration', '繁殖期与水温变化推动盲生群落在深潭、浅湾和岩缝间迁移。'),
        ),
        interactions=(
            ('waterline_mineral_reading', '水痕、盐壳颜色与气泡节律可用于判断潮位和喷口活动层。'),
            ('current_layer_routes', '水面、潜流、淹没矿道和高处岩脊构成受潮次影响的不同通路。'),
            ('cave_foodweb_balance', '扰动喷口、捕食群或附岩生物会连带改变洞海营养流和栖地分布。'),
        ),
    ),
    'gloomwood': _profile(
        'hidden.gloomwood',
        '幽暗密林是野狼林道深处由月相狼群、遮光古木与菌根网络共同维持的夜行林域。',
        drivers=(
            ('eclipse_packs', '适应暗月的狼群按育幼、狩猎和仪式嚎叫重划领地。'),
            ('closed_canopy', '高密树冠控制月光斑、湿度和林下植物的生长窗口。'),
            ('mycorrhizal_web', '古木根系与发光菌丝交换养分并传递林地扰动。'),
        ),
        resources=(
            ('lunar_canid_shed', '夜行狼群换毛、蜕齿、骨质更新与巢材形成的生物材料谱系。'),
            ('shade_fungi', '依赖暗湿木质和狼群残余的菌丝、孢体与发光代谢物谱系。'),
            ('nightwood_resin', '遮光古木受伤、授粉和季节休眠产生的树脂、纤维与种实谱系。'),
        ),
        evolutions=(
            ('pack_inheritance', '狼王更替和分群会迁移核心巢域，而非让同一巢点无限恢复。'),
            ('eclipse_bloom', '月相与云层变化驱动菌群、夜花和猎物形成错峰繁盛。'),
            ('canopy_gap_cycle', '风倒木打开光隙后先锋植物进入，随后树冠重新闭合。'),
        ),
        interactions=(
            ('moon_scent_map', '月光斑、气味线与嚎叫回声共同标出狼群活动层。'),
            ('root_canopy_routes', '根穴、林下兽径和枝冠通道对应不同生物与材料来源。'),
            ('prey_balance', '绕行、投喂或驱散猎物都会改变狼群和菌根网络的后续分布。'),
        ),
    ),
    'starlit_firefly_valley': _profile(
        'hidden.starlit_firefly_valley',
        '星萤花谷是野狼林道深处由夜放花群、发光昆群与谷地溪流共同维持的月夜谷地。',
        drivers=(
            ('nocturnal_bloom', '夜放花群随月相、温湿与林缘光照错峰开合，改变花粉和气味的分布。'),
            ('luminous_swarms', '发光昆群在授粉、迁飞和繁殖阶段重组成明灭不定的谷地光带。'),
            ('valley_streams', '溪流、浅滩与夜雾搬运营养和种实，并持续改写谷底通行面。'),
        ),
        resources=(
            ('floral_condensates', '花蜜、露液、芳香脂和附着花粉形成的夜花分泌物谱系。'),
            ('luminous_insect_shed', '发光昆群留下的蜕壳、鳞粉、丝囊和巢材形成的生物材料谱系。'),
            ('riparian_growth', '溪岸根茎、水草、苔衣和冲积有机物形成的滨水材料谱系。'),
        ),
        evolutions=(
            ('moonphase_succession', '月相变化会移动盛花区、昆群核心和夜行捕食者的活动边界。'),
            ('floodplain_shift', '涨水、改道与沉积会淹没旧花床，并在别处形成新的湿润台地。'),
            ('pollinator_migration', '昆群迁徙会让部分花群衰退，同时把授粉中心带向新的谷坡。'),
        ),
        interactions=(
            ('light_trail_reading', '光带密度、飞行高度与熄亮节律可用于辨认昆群活动层。'),
            ('bloom_window_timing', '不同花群的开合窗口会改变气味、视野与谷中生物的行为。'),
            ('streambank_corridors', '溪面、浅滩、花坡和林缘兽径对应不同生物与材料来源。'),
        ),
    ),
    'bannerless_fort': _profile(
        'hidden.bannerless_fort',
        '无旗军堡是荒野哨站后方由失去番号的军团、陨铁工坊与匿名军纪维系的占领据点。',
        drivers=(
            ('nameless_cohorts', '不同来历的士兵以临时军衔和共同守则维持无旗编制。'),
            ('meteor_forge', '陨铁坯料、军械炉和淬火水决定工坊的材料循环。'),
            ('erased_allegiance', '被刮除的徽记、密封命令和旧敌关系不断重写军堡身份。'),
        ),
        resources=(
            ('meteoric_workstock', '陨铁碎料、锻造边角和与其共生的高温合金材料谱系。'),
            ('anonymous_armory', '去除番号的甲片、兵器构件、旗杆与修理备件谱系。'),
            ('garrison_provisions', '轮值军粮、战地药材、皮革和燃料在封锁中形成的补给谱系。'),
        ),
        evolutions=(
            ('cohort_succession', '一支无名队伍溃散后，幸存者或新军团会接替营区并改写军纪。'),
            ('siege_repair_cycle', '攻防破坏使材料转入抢修，停战期再转向武器和储备。'),
            ('oath_realignment', '新证据和利益会让个人誓约重新结盟，改变门禁与仓储权限。'),
        ),
        interactions=(
            ('rank_without_banners', '口令、轮值和装备磨损比旗号更能揭示真实指挥链。'),
            ('fortification_circuit', '城门、锻坊、兵库和地道构成相互制约的防务回路。'),
            ('muster_negotiation', '可围绕守墙、补给、身份承认或撤离与各队伍交涉。'),
        ),
    ),
    'beast_migration_valley': _profile(
        'hidden.beast_migration_valley',
        '千兽迁徙谷是荒野哨站外缘由季候兽潮、水草走廊与食腐群落共同塑造的开阔迁徙通道。',
        drivers=(
            ('seasonal_megaherds', '不同体型的兽群随繁殖、换角和避寒周期分批穿越谷地。'),
            ('water_grass_corridor', '泉眼、季节河与草场承载力决定兽潮停留、绕行和分流的位置。'),
            ('scavenger_followers', '食腐兽、清理昆群与伺机捕食者沿兽潮前后形成移动食物网。'),
        ),
        resources=(
            ('migration_shed_keratin', '长途行进中自然换落的角质、蹄片、毛束与旧巢材形成的生物材料谱系。'),
            ('trampled_seed_soil', '兽蹄翻动的表土、携带种实与富集有机物形成的草场更新材料谱系。'),
            ('scavenger_processed_remains', '食腐和分解过程留下的净骨、甲壳、消化石与菌群介质谱系。'),
        ),
        evolutions=(
            ('migration_front_shift', '领头兽群和幼兽批次更替会让迁徙前锋、休息带与尾群不断前移。'),
            ('drought_route_change', '水源衰退或复涌会弃用旧兽道，并使支谷成为新的通行主轴。'),
            ('grazing_succession', '重度啃食后的低草、先锋灌丛与恢复草甸会按扰动强度接替。'),
        ),
        interactions=(
            ('track_layer_reading', '足迹叠压、粪迹新旧与折草方向可区分兽潮批次和移动速度。'),
            ('herd_spacing', '领地距离、护幼队形和捕食压力会改变接近兽群的安全边界。'),
            ('corridor_passage', '高坡瞭望、河床横穿、草场边缘和兽道间隙提供不同的穿越条件。'),
        ),
    ),
    'sunken_altar': _profile(
        'hidden.sunken_altar',
        '沉没祭坛是迷雾沼泽水下由潮汐礼仪、浸没信众与祭器生境共同维持的仪式洼地。',
        drivers=(
            ('tidal_liturgy', '涨落水位依次淹没祭阶，使仪式空间按潮次启闭。'),
            ('drowned_congregation', '水生祭司、遗留信众和湿地生物争夺祭坛解释权。'),
            ('votive_biochemistry', '供物、沼雾和厌氧淤泥反应，形成区别于普通湿地的化学梯度。'),
        ),
        resources=(
            ('ritual_silt', '香料、灰烬、金属盐与腐殖物沉积形成的祭仪淤泥谱系。'),
            ('mist_accretion', '雾滴在祭器、藻膜和多孔石材上缓慢富集的珠状材料谱系。'),
            ('aquatic_votive', '被水生群落改造的供物纤维、贝壳、油脂与容器残料谱系。'),
        ),
        evolutions=(
            ('inundation_exposure', '潮水封住上层供台时会暴露深槽，退水后次序反转。'),
            ('congregation_succession', '旧信众消退后，新祭司、捕食者或拾荒者会占用不同祭阶。'),
            ('altar_biocorrosion', '藻膜、酸性水和祭火残余持续蚀刻并重塑祭器表面。'),
        ),
        interactions=(
            ('waterline_geometry', '祭阶高度、虹吸通道和水面反光共同决定可达区域。'),
            ('votive_exchange', '供奉、归还或挪动祭物会影响信众关系和局部生境。'),
            ('reed_underwater_routes', '芦苇缝、水下廊道与气穴形成三套不同移动路径。'),
        ),
    ),
    'mirror_lotus_water_garden': _profile(
        'hidden.mirror_lotus_water_garden',
        '镜莲水庭是迷雾沼泽深处由澄水循环、层叠莲台与沉水石庭共同维持的湿地庭园。',
        drivers=(
            ('clearwater_cycle', '地下清泉、沼泽漫流与莲根过滤共同维持水庭各层不同的透明度。'),
            ('lotus_terraces', '镜莲、浮叶植物与水岸根网沿石台高度形成层叠的光照生境。'),
            ('submerged_masonry', '沉水栏桥、泄水孔与旧庭墙引导水流，并为水生群落提供附着面。'),
        ),
        resources=(
            ('lotus_tissues', '镜莲叶蜡、花露、莲丝、种实与枯落纤维形成的植物材料谱系。'),
            ('mineral_films', '清泉矿盐、藻膜和石庭表面缓慢析出的透明薄层材料谱系。'),
            ('silted_gardenwork', '淤泥中保存的园艺构件、贝壳、陶片和修庭介质形成的遗存谱系。'),
        ),
        evolutions=(
            ('waterlevel_reveal', '水位升降会轮流淹没高低莲台，并显露不同年代的石庭结构。'),
            ('terrace_overgrowth', '无人修整的莲根会占据旧水道，清障后又会在别处形成新密丛。'),
            ('current_rechanneling', '泄水孔堵塞或开启会改变澄水路径，使清浊分界随之迁移。'),
        ),
        interactions=(
            ('reflection_observation', '倒影完整度、叶面扰动和水下折光可揭示流速与隐没结构。'),
            ('terrace_navigation', '浮叶面、浅水石台、沉桥和芦岸构成高度不同的接近路线。'),
            ('wetland_balance', '移植、疏通或扰动莲群都会改变水质、栖地与后续可达区域。'),
        ),
    ),
    'silent_execution_ground': _profile(
        'hidden.silent_execution_ground',
        '无声刑场是叹息地牢尽头由消声刑具、判决档案与被压抑证词维持的处刑设施。',
        drivers=(
            ('silence_ward', '吸声墙、禁言锁和仪式边界共同压制声音与讯息传播。'),
            ('execution_apparatus', '刑台、输送道和清理设施按旧处刑流程形成物质流。'),
            ('suppressed_testimony', '囚犯刻痕、看守记录和残留回声保存互相冲突的证词。'),
        ),
        resources=(
            ('silence_lockwork', '消声锁具、吸振构件、封口材料与维护残件形成的机关谱系。'),
            ('execution_salvage', '刑台金属、绳索纤维、磨石和清理工具产生的设施材料谱系。'),
            ('forensic_archive', '判决标记、身份牌、组织残留与防腐介质形成的证据谱系。'),
        ),
        evolutions=(
            ('suppression_decay', '禁言设施失衡会让部分声区恢复，同时把寂静压力转移到别处。'),
            ('executioner_succession', '旧行刑者消失后，看守、囚徒或新占领者会重用并改造刑场。'),
            ('testimony_resurfacing', '封层破损和档案互证会逐步释放旧证词，而非重复同一回声。'),
        ),
        interactions=(
            ('silence_gradients', '振动、唇语和物体反馈可区分完全消声区与泄漏边界。'),
            ('scaffold_routes', '囚道、看守台、器械槽和清理沟形成权限不同的通路。'),
            ('witness_chain', '记录、刻痕与幸存回声需按来源互证才能改变刑场秩序。'),
        ),
    ),
    'echo_organ_cavern': _profile(
        'hidden.echo_organ_cavern',
        '回声风琴洞是叹息地牢裂隙后方由管状钟乳石、压差洞风与回声定位生物共同维持的鸣响洞系。',
        drivers=(
            ('organ_stalactite_array', '中空石管与共鸣石柱按长度、裂隙和含水量形成不同音域。'),
            ('pressure_driven_airflow', '地牢内外的温压差推动洞风换向，使各支洞轮流成为发声主腔。'),
            ('echolocating_colonies', '依靠回声活动的翼兽、穴居群落与伏击者围绕频段划分生态位。'),
        ),
        resources=(
            ('resonant_cavity_minerals', '空腔晶簇、薄壁矿壳和受振动定向生长的矿物形成的声学材料谱系。'),
            ('acoustic_fauna_shed', '回声生物换落的翼膜、听骨、触须壳与育幼巢材形成的生物材料谱系。'),
            ('airflow_deposits', '洞风搬运的细尘、盐霜、孢子与凝结水形成的沉积材料谱系。'),
        ),
        evolutions=(
            ('stone_pipe_growth_breakage', '滴水增生和过强共振会分别延长或折断石管，持续改写洞系音域。'),
            ('airflow_reversal', '季候压差与通道开闭会让主风道反转，并唤醒此前沉寂的共鸣腔。'),
            ('frequency_niche_succession', '优势频段改变后，依赖旧回声窗口的群落退让并由另一批生物接替。'),
        ),
        interactions=(
            ('resonance_mapping', '回响时差、泛音和石管颤动可揭示空腔尺度与相邻通道。'),
            ('sound_pressure_routes', '静音缝、持续鸣腔和风口之间的声压差会改变移动与交流条件。'),
            ('colony_signal_boundary', '模仿、遮蔽或扰乱回声信号会影响群落警戒、觅食和领地反应。'),
        ),
    ),
    'holy_reliquary_catacomb': _profile(
        'hidden.holy_reliquary_catacomb',
        '圣骸墓窟是废弃修道院地下由圣骸保存、守墓礼序与磷光矿化共同维持的藏骨层。',
        drivers=(
            ('relic_custody', '守墓者、朝圣者与自律器物围绕圣骸保管权限形成秩序。'),
            ('sacred_taphonomy', '防腐、骨质分解和地下水渗滤共同改变墓窟介质。'),
            ('pilgrim_trace', '祷告、油灯、花束与修复行为不断给墓室留下新的使用层。'),
        ),
        resources=(
            ('ossuary_mineral', '骨盐、磷质沉积和围绕遗骸生长的矿物材料谱系。'),
            ('reliquary_conservation', '裹布、封蜡、香脂、金属匣与修补介质形成的保存材料谱系。'),
            ('funerary_biota', '墓园根系、耐暗苔藓、蜂蜡残留和分解菌群形成的生物谱系。'),
        ),
        evolutions=(
            ('custodian_succession', '看护者更替会改变开放墓室、修复优先级和朝圣路径。'),
            ('phosphate_migration', '地下水改道使旧矿化点停滞，并在别的骨层形成新沉积。'),
            ('pilgrimage_disturbance', '朝圣繁盛、禁绝或盗扰分别推动清理、封闭和再安置。'),
        ),
        interactions=(
            ('veneration_protocols', '墓志、礼序和守护誓言决定哪些区域可接近或修复。'),
            ('ossuary_stratigraphy', '骨位、沉积层和包覆材料可用来判断年代与材料来源。'),
            ('reliquary_access', '匣锁、祭台、运骨廊和通风孔构成不同的保管边界。'),
        ),
    ),
    'serpent_matriarch_palace': _profile(
        'hidden.serpent_matriarch_palace',
        '蛇母蜕宫是剧毒钟乳洞深层由蛇母蜕变、蛇裔等级与毒性矿宫共同维持的繁殖宫室。',
        drivers=(
            ('matriarch_molting', '蛇母的生长期、蜕期与休眠期重新分配热源和护卫。'),
            ('brood_hierarchy', '卵室、幼蛇、侍卫与成年争位者构成分层蛇裔社会。'),
            ('venom_mineral_palace', '毒液、钟乳水和体温使宫壁产生特有的矿物膜。'),
        ),
        resources=(
            ('royal_molt_family', '蛇母及近侍自然蜕落的皮膜、鳞片与透明结构材料谱系。'),
            ('venom_gland_cycle', '不同年龄蛇裔分泌、稀释和废弃的毒性生物材料谱系。'),
            ('brood_nest_matrix', '卵膜、巢垫、热石与抗毒菌毯形成的育幼材料谱系。'),
        ),
        evolutions=(
            ('molt_chamber_rotation', '一次蜕变结束后旧宫室转为育幼或废弃，新蜕室在暖层形成。'),
            ('brood_rank_succession', '幼体成熟、护卫败亡或争位会改变各宫室占领者。'),
            ('toxic_mineral_growth', '渗水和毒液比例变化使旧矿膜钝化并在新表面继续沉积。'),
        ),
        interactions=(
            ('pheromone_court', '气味、蜕皮新旧和身体姿态共同表达宫廷等级。'),
            ('coil_verticality', '盘柱、暖井、卵室和水槽形成垂直交错的蛇行路线。'),
            ('brood_appeasement', '保护、交换或惊扰卵与热源会改变蛇裔群体的长期态度。'),
        ),
    ),
    'scorpion_king_catacomb': _profile(
        'hidden.scorpion_king_catacomb',
        '蝎王地宫是狂沙荒漠地下由王蝎群落、沙葬墓制与毒素矿化共同维持的穴墓复合体。',
        drivers=(
            ('royal_scorpion_colony', '王蝎、育幼群、工蝎与挑战者围绕巢室形成等级群落。'),
            ('sand_tomb_microclimate', '墓墙蓄热、沙层含水与通风竖井塑造稳定地下气候。'),
            ('venom_mineralization', '毒液、盐分和墓室金属缓慢反应并在尾针与壁缝沉积。'),
        ),
        resources=(
            ('stinger_mineral_family', '蝎群尾针更新、毒液干涸与盐析形成的毒性晶质材料谱系。'),
            ('carapace_shed', '王蝎群落蜕壳、卵鞘和巢垫留下的甲壳生物材料谱系。'),
            ('funerary_salt_metal', '陪葬金属、保存盐和沙磨碎屑形成的墓葬材料谱系。'),
        ),
        evolutions=(
            ('colony_succession', '王蝎衰亡后挑战者接位，育幼室和猎场随群落中心迁移。'),
            ('sand_ingress_cycle', '沙暴压力让部分墓道灌沙，同时从别处抽空并暴露旧层。'),
            ('toxin_stabilization', '活性毒物随时间钝化，新的分泌和矿盐反应在别处形成。'),
        ),
        interactions=(
            ('vibration_map', '沙面振动、墙体共鸣和尾针敲击揭示群落边界。'),
            ('burrow_tomb_layers', '天然蝎穴、人工墓道和竖井热层构成三套空间逻辑。'),
            ('colony_rank_signals', '气味、蜕壳位置和猎物分配可用于判断或影响蝎群等级。'),
        ),
    ),
    'azure_mirage_sea': _profile(
        'hidden.azure_mirage_sea',
        '碧落蜃海是狂沙荒漠腹地由深层含水带、热力折光与迁移沙丘共同维持的蓝色幻海。',
        drivers=(
            ('deep_aquifer', '断层泉眼与深层含水带间歇上涌，在沙海中形成可迁移的湿润核心。'),
            ('thermal_refraction', '地表温差、泉汽和细砂悬尘共同折射天光，改变远近与水陆观感。'),
            ('migrating_dunes', '风向与粒径分选推动沙丘缓慢迁徙，反复掩埋或释放泉缘地貌。'),
        ),
        resources=(
            ('glass_sand_family', '高温表层、矿物细砂与泉汽侵蚀共同形成的琉璃质颗粒谱系。'),
            ('saline_crusts', '泉水蒸发后析出的盐壳、矿膜和吸湿结晶形成的蒸发材料谱系。'),
            ('oasis_biota', '耐旱根茎、藻毯、虫蜕与迁徙生物遗留形成的泉缘生物谱系。'),
        ),
        evolutions=(
            ('spring_migration', '地下水压改变会让旧泉眼衰弱，并在新的断层低点形成湿地。'),
            ('dune_burial', '沙丘推进会封住部分岸线，同时从背风坡剥露更早的沉积层。'),
            ('refraction_shift', '昼夜温差和悬尘浓度变化会重排蜃景层次与可见地标。'),
        ),
        interactions=(
            ('horizon_triangulation', '风纹、星位、倒影偏差和固定岩脊可共同校准幻海中的方位。'),
            ('water_quality_judgment', '盐壳色泽、藻相与蒸汽气味可区分不同泉系及其变化。'),
            ('oasis_water_rights', '迁徙者、泉缘生物与采集者可能围绕有限水道形成动态边界。'),
        ),
    ),
    'eye_of_thunder': _profile(
        'hidden.eye_of_thunder',
        '雷霆之眼是风暴尖塔上空由静风眼、导雷环与雷暴生物共同维持的高势核心。',
        drivers=(
            ('storm_eye_pressure', '气压差和旋转云墙周期性收缩、偏移并重建短暂平静区。'),
            ('conductor_ring', '塔冠导体、悬浮碎片和接地链共同分配放电路径。'),
            ('thunder_biota', '耐电飞行生物与元素群落追随上升气流和电荷梯度。'),
        ),
        resources=(
            ('storm_condensate', '云滴、离子和导体表面析出物形成的雷暴凝聚材料谱系。'),
            ('fulgurite_network', '放电熔结塔石、尘粒和金属形成的电熔矿物谱系。'),
            ('aerial_shed', '雷暴生物换羽、巢材、绝缘分泌物与卵膜形成的生物谱系。'),
        ),
        evolutions=(
            ('eye_precession', '风暴眼沿塔体进动，使旧静区进入云墙并在别层打开新窗口。'),
            ('discharge_recharge', '一次放电耗散局部电势，云流随后在不同导体间重新蓄积。'),
            ('conductor_repair', '导体熔断、坠落或被生物筑巢后，电流会改走新回路。'),
        ),
        interactions=(
            ('calm_window_timing', '需结合云墙速度、气压和放电间隔判断短暂通行窗。'),
            ('potential_network', '接地、悬浮导体和绝缘层的关系决定风险与材料位置。'),
            ('flight_arc_routes', '上升气流、雷暴生物航线和碎片背风面提供不同接近方式。'),
        ),
    ),
    'cloudtop_flower_garden': _profile(
        'hidden.cloudtop_flower_garden',
        '云上花庭是风暴尖塔高处由风眼缓流、雷养花群与悬空台地共同维持的高空庭园。',
        drivers=(
            ('calm_eye_airflow', '风暴眼边缘的缓流、上升气柱与背风涡旋共同划出短暂稳定的花带。'),
            ('lightning_nourished_flora', '耐电花草借微弱放电、雨水和云尘获得养分，并沿电势分层生长。'),
            ('floating_terraces', '轻质岩台、缠根藤桥与古旧塔构件在高空中形成漂移的栖地群。'),
        ),
        resources=(
            ('aerial_fibers', '高空花茎、韧藤、绒籽与飞行生物换落轻羽形成的纤维谱系。'),
            ('charged_pollen', '雷养花群释放的花粉、蜜露和附电种实形成的植物分泌物谱系。'),
            ('cloudwater_minerals', '云滴、塔尘与岩台孔隙析出物形成的轻质矿物材料谱系。'),
        ),
        evolutions=(
            ('stormfront_shift', '风暴锋面移动会压缩旧缓流区，并在别的塔层打开新的稳定窗口。'),
            ('terrace_drift', '岩台受风与根网牵引缓慢错位，使花带、桥路和巢区重新相接。'),
            ('bloom_charge_cycle', '放电积累与耗散会推动不同花群轮流盛放、结籽和休眠。'),
        ),
        interactions=(
            ('windpath_observation', '花粉走向、云丝弯曲和飞行生物轨迹可显示高空风路。'),
            ('charge_boundary_handling', '花冠亮度、根网颤动与岩面微光可提示电势边界的迁移。'),
            ('terrace_garden_claims', '护花者、迁徙生物与采集者可能围绕花台用途形成临时秩序。'),
        ),
    ),
    'faceless_gallery': _profile(
        'hidden.faceless_gallery',
        '无面回廊是暗影迷宫内由抹除身份的镜厅、无面看守与流动反射共同维持的陈列空间。',
        drivers=(
            ('identity_erasure', '镜面会分离来访者的面貌、称谓和自我记忆并保存为展品痕迹。'),
            ('faceless_curators', '看守、模仿者和失去身份的访客围绕展位维护不同规则。'),
            ('reflective_flow', '镜银、阴影和观察角度使门廊与陈列位置持续重排。'),
        ),
        resources=(
            ('mirror_metal_family', '镜背合金、流动反射层和抛光残料形成的镜质材料谱系。'),
            ('discarded_visages', '脱离载体的面具膜、表情痕迹和拟态组织形成的身份介质谱系。'),
            ('memory_pigment', '被镜面吸附的书写、颜料、影像与记忆沉积形成的信息材料谱系。'),
        ),
        evolutions=(
            ('portrait_replacement', '新身份被收录后，旧展品会移位、合并或被看守封存。'),
            ('mirror_reflow', '破镜和视线改变反射连续性，使镜银在其他墙面重新聚集。'),
            ('curator_succession', '看守失效后，模仿者或被困访客会接管陈列规则。'),
        ),
        interactions=(
            ('reflection_tests', '动作延迟、倒影缺失和多视角核对可辨认真实通路。'),
            ('perspective_routes', '同一回廊从正视、侧视和镜后进入时呈现不同连接。'),
            ('identity_bargains', '取回、代存或承认某段身份会改变无面看守的权限判断。'),
        ),
    ),
    'daynight_boundary_court': _profile(
        'hidden.daynight_boundary_court',
        '昼夜交界庭是暗影迷宫内由移动光界、趋光影蛾与双相庭园植物共同维持的明暗交替庭域。',
        drivers=(
            ('moving_light_boundary', '来自迷宫缝隙的光带沿庭域缓慢偏移，让昼侧与夜侧不断交换位置。'),
            ('phototactic_shadow_moths', '影蛾群在追光、避亮和繁殖阶段围绕交界线形成不同飞行层。'),
            ('dual_phase_flora', '一批植物只在明侧展开，另一批在暗侧活跃，并通过根网交换养分。'),
        ),
        resources=(
            ('bicolor_moth_shed', '影蛾换落的双色鳞屑、茧膜、触须壳与附着花粉形成的生物材料谱系。'),
            ('phase_garden_tissue', '明生叶片、夜放花器、共享根茎与休眠芽形成的庭园植物谱系。'),
            ('boundary_condensates', '冷热交界凝成的露液、光敏盐霜与阴面菌膜形成的界面材料谱系。'),
        ),
        evolutions=(
            ('boundary_precession', '光源角度与迷宫结构变化会推动明暗分界环绕庭域迁移。'),
            ('moth_brood_migration', '不同羽化批次会追随适宜光度迁巢，使旧茧区转为捕食或分解带。'),
            ('dual_bloom_succession', '昼侧植物退入休眠后夜侧群落接替，根网仍保留前一阶段的养分影响。'),
        ),
        interactions=(
            ('illumination_timing', '叶片开合、影长与露点变化可用于判断光界移动速度和停留窗口。'),
            ('shadow_moth_signals', '鳞光闪烁、群飞方向与触须振动可揭示影蛾的警戒和繁殖状态。'),
            ('phase_garden_routes', '明侧花台、交界根廊与暗侧菌径提供随光界变化的不同接近方式。'),
        ),
    ),
    'eternal_frost_royal_tomb': _profile(
        'hidden.eternal_frost_royal_tomb',
        '永冻王墓是冰霜峡谷内由王室冷葬制度、冻存遗骸与寒地守陵群落维持的低温墓宫。',
        drivers=(
            ('royal_cryo_rites', '不同墓室按王室葬序保持特定温度、气流和封存方式。'),
            ('frozen_taphonomy', '冰压、盐析和极慢分解共同保存并改变遗骸与随葬物。'),
            ('cold_custodians', '守陵构造物、寒地兽群和后继祭司争夺保温设施。'),
        ),
        resources=(
            ('cryopreserved_tissue', '冻存骨质、髓质、角蛋白和防腐有机物形成的生物材料谱系。'),
            ('funerary_frostwork', '墓室冰砖、冷锻金属、盐晶与导冷构件形成的工艺材料谱系。'),
            ('seal_insulation', '封蜡、裹布、树脂、毛毡与真空容器残件形成的保温谱系。'),
        ),
        evolutions=(
            ('thermal_chamber_shift', '裂隙和门禁改变热流，使旧冻室融化并让别处重新深冻。'),
            ('dynastic_reinterpretation', '后继守陵者会重排墓主次序、封号和祭祀资源。'),
            ('cold_scavenger_succession', '解冻区先吸引腐食者，重新封冻后耐寒物种接替。'),
        ),
        interactions=(
            ('temperature_zones', '霜纹、气流和材料脆性揭示墓室间的温度层级。'),
            ('genealogy_seals', '谱牒、印记和陪葬次序决定王墓门禁与守陵者认定。'),
            ('preservation_choices', '修复冷封、短时解冻或搬移遗物会留下不同长期后果。'),
        ),
    ),
    'aurora_mirror_lake': _profile(
        'hidden.aurora_mirror_lake',
        '极光镜湖是冰霜峡谷深处由极光能流、冰下暖泉与层压湖冰共同维持的寒地湖盆。',
        drivers=(
            ('auroral_flux', '极光带来的带电粒子沿山脊与冰面传导，使湖区光色和电势持续变化。'),
            ('subglacial_springs', '冰下暖泉输送矿物与热量，在封冻湖盆中维持不规则的水交换。'),
            ('pressure_ice', '风压、冻胀与水位推挤层冰，形成会生长、断裂和复合的透明结构。'),
        ),
        resources=(
            ('layered_ice_fibers', '反复冻融与挤压析出的冰晶薄片、纤维和夹层气泡形成的冰质谱系。'),
            ('thermal_mineral_blooms', '暖泉矿盐、微生物膜和湖底沉积形成的温泉矿物谱系。'),
            ('coldwater_biota', '冰下藻类、耐寒水生物与季节性繁殖残留形成的寒湖生物谱系。'),
        ),
        evolutions=(
            ('freeze_thaw_mosaic', '暖泉脉动和寒潮交替会关闭旧薄冰区，并在别处形成新裂隙。'),
            ('auroral_charge_shift', '极光强弱与山脊导流变化会移动湖面的发光带和带电区域。'),
            ('spring_channel_migration', '冰下沉积堵塞旧泉道后，热流会沿新的湖底裂缝重新分配。'),
        ),
        interactions=(
            ('ice_resonance_reading', '敲击回声、霜纹和气泡层次可辨认冰层厚度与内部空腔。'),
            ('thermal_edge_crossing', '雾带、水色与融冰轮廓会显示暖泉区和稳定冻面的边界。'),
            ('seasonal_habitat_balance', '凿冰、引流或封堵会改变湖中栖地、光路与后续冰况。'),
        ),
    ),
    'blood_feast_cellar': _profile(
        'hidden.blood_feast_cellar',
        '血宴地窖是食人魔要塞下由氏族宴制、发酵窖池与巨型猎物处理共同维持的地下食物系统。',
        drivers=(
            ('clan_feast_cycle', '胜负、季节和首领威望决定宴会规模与地窖配给。'),
            ('fermentation_ecology', '酵母、霉菌、盐水和温度分层控制储藏物的转化。'),
            ('giant_butchery', '大型猎物、战利遗骸与屠宰设施产生连续的组织和骨料流。'),
        ),
        resources=(
            ('giant_osseous_family', '巨型生物的骨、角、腱和自然风干组织形成的材料谱系。'),
            ('cellar_culture', '酒曲、酸菌、霉皮、盐卤和吸附木桶形成的发酵材料谱系。'),
            ('butcher_siege_salvage', '屠具、挂链、砧台与退役攻城构件形成的重型器材谱系。'),
        ),
        evolutions=(
            ('feast_depletion_refill', '一场血宴会消耗特定窖区，下一轮狩猎从不同来源补入。'),
            ('culture_succession', '温湿度变化使酵母、酸菌和腐败群落依次接替。'),
            ('cellar_occupation', '氏族夺权后会重新分配酒窖、骨库和禁食区。'),
        ),
        interactions=(
            ('larder_hierarchy', '首领酒窖、公共肉库、发酵池和废料沟具有不同权属。'),
            ('scent_heat_channels', '气味、热气与渗液路径可追踪加工阶段和生物活动。'),
            ('tribute_bargains', '提供猎物、维护窖池或挑战配额能改变氏族对来访者的态度。'),
        ),
    ),
    'chaos_throne': _profile(
        'hidden.chaos_throne',
        '混沌王座是遗忘废墟中由现实变形的座基、争位者与不稳定锚点共同维持的权力核心。',
        drivers=(
            ('reality_warping_seat', '王座会随占有者和观察方式改变附近材料与空间规则。'),
            ('claimant_conflict', '遗民、畸变生物和外来强者不断争夺坐席及解释权。'),
            ('ruin_anchors', '残柱、铭文与稳定器把部分空间固定，限制混沌扩散。'),
        ),
        resources=(
            ('metamorphic_throne_matter', '座基剥落、重组和相变留下的异质矿物与构件材料谱系。'),
            ('spatial_residue', '折叠边界、位移摩擦和稳定锚周围沉积的空间介质谱系。'),
            ('contested_regalia', '历代争位者遗留、改造或弃置的冠饰、武备和权杖构件谱系。'),
        ),
        evolutions=(
            ('geometry_mutation', '一个区域被稳定后，形变压力会转移并在其他廊段重组。'),
            ('claimant_succession', '争位者败亡或退让后，新占有者会按自身秩序改造王座圈。'),
            ('stability_window', '锚点消耗、修复与共振形成暂时可预测的空间阶段。'),
        ),
        interactions=(
            ('observation_effects', '视角、命名和测量顺序可能改变局部形态，需交叉验证。'),
            ('authority_contest', '承认、挑战或分割权力会影响争位者与王座的响应。'),
            ('anchor_routes', '沿稳定锚移动较安全，但会错过只在形变区出现的材料层。'),
        ),
    ),
    'earthfire_molten_heart': _profile(
        'hidden.earthfire_molten_heart',
        '地火熔心是熔岩火山腹内由岩浆脉动、火生群落与古老炉心看护共同维持的热源腔。',
        drivers=(
            ('magma_plumbing', '岩浆囊、气体通道和结晶前缘共同决定热流与压力。'),
            ('pyrobiological_colony', '耐火菌毯、熔岩虫群和元素生物依附不同温度带。'),
            ('hearth_custody', '炉心守护者、锻造遗制和引火设施调节局部燃烧。'),
        ),
        resources=(
            ('persistent_flame_family', '由燃料、矿气和看护仪式维持的火种与燃烧介质谱系。'),
            ('magmatic_differentiates', '岩浆冷却分异形成的晶体、玻璃、金属硫化物与炉渣谱系。'),
            ('thermophile_shed', '火生群落蜕落的耐热壳膜、菌毯和分泌物形成的生物谱系。'),
        ),
        evolutions=(
            ('pressure_pulse', '一次泄压会冷却旧喷口，并让别的裂隙成为新热流通道。'),
            ('crust_reopening', '熔壳凝固形成通路，后续升温又会熔穿或改道。'),
            ('fire_colonization', '新冷却面先被菌毯占据，升温后耐火虫群和元素接替。'),
        ),
        interactions=(
            ('thermal_strata', '气腔、熔面、冷壳和深部炉心对应不同材料与生物。'),
            ('gas_flow_reading', '火焰颜色、气泡和岩壁振动可判断气体与岩浆走向。'),
            ('tending_or_tapping', '维护炉心、疏导压力或抽取热量会改变后续热源分布。'),
        ),
    ),
    'inverted_imperial_tomb': _profile(
        'hidden.inverted_imperial_tomb',
        '倒悬帝陵是古王陵寝深处由逆重力葬制、帝国地脉与悬置守陵设施共同维持的倒置墓域。',
        drivers=(
            ('inverted_gravity', '定向重力场让尘埃、水流和遗物向墓顶聚集并分层。'),
            ('imperial_geomancy', '龙脉、方位印与帝陵礼制共同约束墓室朝向和门禁。'),
            ('suspended_custody', '悬桥、配重器与守陵构造物在倒置空间继续执行职责。'),
        ),
        resources=(
            ('reverse_grown_jade', '受逆重力流体和地脉矿化影响生长的玉质与伴生矿物谱系。'),
            ('hanging_funeral_fittings', '悬棺连接件、丝索、金属扣与浮置祭器构成的工艺谱系。'),
            ('geomantic_sediment', '香灰、矿尘和水盐沿反向沉降形成的地脉沉积谱系。'),
        ),
        evolutions=(
            ('gravity_field_migration', '阵眼衰减或修复会移动重力边界，使旧悬层落定并产生新倒置区。'),
            ('ward_succession', '王朝守陵者失去控制后，新祭官或墓中居民会重编门禁。'),
            ('ceiling_erosion', '水与碎屑由下向上侵蚀墓顶，再随场域变化重新堆积。'),
        ),
        interactions=(
            ('orientation_mapping', '必须同时记录礼制方位和局部重力方向才能辨认路线。'),
            ('imperial_seal_order', '爵位印、祭次和墓主关系决定守陵设施的响应。'),
            ('counterweight_network', '悬桥、配重与浮置器物可被调整，但会连带改变其他墓室。'),
        ),
    ),
    'starvault_funeral_bloom': _profile(
        'hidden.starvault_funeral_bloom',
        '星穹葬花原是古王陵寝穹顶下由地下星图、葬花周期与墓层水脉共同维持的幽光花原。',
        drivers=(
            ('subterranean_star_chart', '穹顶矿晶与导光孔组成缓慢轮转的星图，控制花原各带的光照节律。'),
            ('funeral_bloom_cycle', '葬花依次萌发、盛放、散粉和归根，把陵寝有机物转入下一季。'),
            ('tomb_layer_aquifer', '墓层渗水沿棺室、石渠和根网分配水分与溶解矿盐。'),
        ),
        resources=(
            ('stellar_pollen_seed', '受星图光序影响的花粉、种实、果荚与休眠胚形成的繁衍材料谱系。'),
            ('funerary_root_fiber', '穿行墓土的根纤维、共生菌索与腐殖结节形成的地下生物材料谱系。'),
            ('starwell_mineral_dew', '导光矿晶、墓层盐分与夜间凝露共同形成的微光沉积谱系。'),
        ),
        evolutions=(
            ('constellation_season_shift', '星图轮转会让盛花带沿不同墓区迁移，旧花原则进入结籽和休眠。'),
            ('bloom_decay_regrowth', '凋落花层经分解回到根网，并在下一轮光序中支持不同种群接替。'),
            ('tomb_watercourse_migration', '石渠堵塞、墓层渗漏或根系扩张会移动湿润带与花群边界。'),
        ),
        interactions=(
            ('star_chart_observation', '穹顶亮点次序、花冠朝向与露光变化可用于辨认当前星图阶段。'),
            ('bloom_phase_timing', '萌发、散粉和闭合阶段对应不同生物活动与墓园通行条件。'),
            ('tomb_garden_stewardship', '疏水、护根或扰动墓土会改变后续花带、水脉和守墓群落关系。'),
        ),
    ),
    'dragon_emperor_tomb': _profile(
        'hidden.dragon_emperor_tomb',
        '龙皇陵是巨龙巢穴最深层由龙皇遗骸、龙裔陵卫与葬藏秩序共同维持的帝龙墓宫。',
        drivers=(
            ('imperial_dragon_remains', '龙皇遗骸的骨、鳞和残余威压塑造墓宫微环境。'),
            ('draconic_retainers', '陵卫龙裔、祭司与觊觎者围绕血统和职责争夺区域。'),
            ('funerary_hoard_order', '葬藏按功绩、年代和誓约排列，并非普通巢穴的散落财货。'),
        ),
        resources=(
            ('ancient_draconic_shed', '帝龙遗骸自然剥落的鳞质、骨质和角质材料谱系。'),
            ('hoard_weathering', '葬藏金属、宝石、容器与龙息环境反应形成的老化材料谱系。'),
            ('funerary_resin', '封棺树脂、龙裔香料、裹布与保存介质构成的陵葬谱系。'),
        ),
        evolutions=(
            ('descendant_occupation', '不同龙裔支系会按血统和誓约接替外陵、祭殿或护骨区。'),
            ('mausoleum_pressure_leak', '封层破损会释放威压并耗散旧节点，在相邻墓室形成新影响区。'),
            ('hoard_stewardship', '陵卫会搬移、修复或封存葬藏，使材料流随职责而变化。'),
        ),
        interactions=(
            ('dominance_signals', '龙语称谓、威压响应和陵卫姿态揭示血统权力关系。'),
            ('burial_stratigraphy', '鳞骨位置、葬藏年代和封存层可核对材料真实来源。'),
            ('oath_and_tribute', '履行旧誓、归还葬物或挑战陵权会产生不同长期后果。'),
        ),
    ),
    'prison_of_ten_thousand_sins': _profile(
        'hidden.prison_of_ten_thousand_sins',
        '万罪监牢是恶魔岛内部由恶魔真名禁制、罪类分区与狱卒派系共同维持的炼狱监禁体系。',
        drivers=(
            ('true_name_incarceration', '真名、契约和刑锁把不同恶魔的能力限制在指定牢区。'),
            ('sin_taxonomy', '罪类、刑期和相互诱发关系决定囚犯与污染的空间分配。'),
            ('warden_factions', '狱卒恶魔、判官与囚徒代理人围绕执行权持续争斗。'),
        ),
        resources=(
            ('name_lock_components', '真名刻印、刑锁金属、封契介质与维修残件形成的禁制谱系。'),
            ('infernal_penal_byproduct', '惩戒过程产生的蜕质、热蚀物、腐化分泌与抑制残余谱系。'),
            ('confession_contract_archive', '罪状、口供、契据副本和判决印形成的信息材料谱系。'),
        ),
        evolutions=(
            ('inmate_faction_succession', '首领被转押或失势后，其他罪类囚犯会接管空牢区。'),
            ('seal_reassignment', '刑锁耗损后狱卒会转移、叠加或重写禁制而非复原同一节点。'),
            ('sin_contamination_transfer', '一种罪性被压制后，其污染会沿契约或通风向别区迁移。'),
        ),
        interactions=(
            ('true_name_authority', '只有精确的名字、判令和签署关系才能影响禁制权限。'),
            ('cell_taxonomy', '牢区按罪类和刑法组织，不能当作同质采集房间。'),
            ('warden_prisoner_bargains', '狱卒、判官和囚徒的契约均有代价与可核对的权责。'),
        ),
    ),
    'twilight_of_the_gods': _profile(
        'hidden.twilight_of_the_gods',
        '诸神黄昏是幽冥王都边界由衰亡神性、暮光潮汐与送葬诸灵共同维持的神陨原野。',
        drivers=(
            ('dying_divinities', '不同神祇的残躯、权柄和消散速度塑造互不相同的领域。'),
            ('twilight_tide', '昼夜边界停滞形成的暮光潮周期覆盖、暴露神陨地层。'),
            ('psychopomp_orders', '引魂者、残存信徒与噬神生物争夺神魂归宿。'),
        ),
        resources=(
            ('fading_divine_condensate', '衰退神性随领域消散凝聚的魂露、辉尘与权柄介质谱系。'),
            ('relic_weathering', '神器外壳、祭器和神殿材料在失去神力后风化的构件谱系。'),
            ('soul_biota', '依附神陨地的魂花、暮兽、菌毯与蜕落物形成的灵性生物谱系。'),
        ),
        evolutions=(
            ('divine_domain_succession', '一位神祇彻底消散后，其领域会被邻近权柄或魂生群落接替。'),
            ('twilight_inundation', '暮光潮推进会淹没旧暴露面，退行时从别处显露残骸。'),
            ('cult_reinterpretation', '新证词与信众更替会改变葬礼和看护方式，但不复活有限遗物。'),
        ),
        interactions=(
            ('pantheon_boundaries', '神徽、自然现象和魂灵行为可识别不同权柄领域。'),
            ('dusk_windows', '暮光潮相位决定可见、可触及和可被引渡的事物。'),
            ('rite_and_custody', '送葬、守护或掠夺神陨遗产会改变引魂者与信徒关系。'),
        ),
    ),
    'infernal_demon_city': _profile(
        'hidden.infernal_demon_city',
        '炼狱魔城是黑日王庭疆域内由恶魔城区、血统政权与地狱炉业共同维持的活体都市。',
        drivers=(
            ('demonic_polity', '贵族、军团、行会与奴役群体按城区争夺税权和暴力。'),
            ('hell_furnace_economy', '地狱炉、燃料债和锻造产业驱动原料与废物在城市流动。'),
            ('infernal_bloodlines', '统治血统的繁殖、夺位和生物特征影响城市制度。'),
        ),
        resources=(
            ('infernal_biological_lineage', '统治种与城市恶魔蜕落的血质、角质、心肌和分泌物材料谱系。'),
            ('hellforge_industry', '炉渣、耐火砖、合金边料与冷却介质形成的工业材料谱系。'),
            ('contract_seal_stock', '税契、奴契、通行印和燃料票据使用的封印介质谱系。'),
        ),
        evolutions=(
            ('dynastic_succession', '统治者败亡后新血统会接管宫区并重分税权与资源。'),
            ('district_reoccupation', '暴乱、清洗或迁徙使城区被不同恶魔群体重新占用。'),
            ('furnace_fuel_cycle', '一座地狱炉停转后产业会迁往别区，旧炉区进入冷却再利用。'),
        ),
        interactions=(
            ('faction_districts', '宫城、炉区、契市和贫窟具有明确群体与资源差异。'),
            ('infernal_infrastructure', '熔渠、烟道、血路和城门共同决定城市物质循环。'),
            ('contract_and_tribute', '交易、纳贡和结盟必须遵循精确契约，违约会改变城区秩序。'),
        ),
    ),
    'space_time_rift': _profile(
        'hidden.space_time_rift',
        '时空裂缝是陨落星都边缘由时间剪切、星质吸积与错位生物共同维持的相位断层。',
        drivers=(
            ('temporal_shear', '不同时间流速和因果方向在裂缝边界产生剪切带。'),
            ('stellar_accretion', '星尘、城市碎片和宇宙介质被相位差持续捕获与释放。'),
            ('displaced_ecologies', '来自异时异地的生物在短暂稳定区适应、竞争或消失。'),
        ),
        resources=(
            ('chronal_stardust', '受时间差分选的星砂、辐照尘与微型陨质形成的天体材料谱系。'),
            ('phase_shed_matter', '物体跨相位时剥离的复制层、老化层和相位膜形成的材料谱系。'),
            ('displaced_salvage', '被裂缝带来的器物、建筑碎片和生存介质形成的错位物资谱系。'),
        ),
        evolutions=(
            ('rift_precession', '断层沿星都缓慢移动，使旧稳定点闭合并在新坐标展开。'),
            ('timeline_overlap', '一条时间线退相干后，另一条会短暂覆盖同一区域。'),
            ('habitat_acclimation', '错位生物若存活会改造稳定区，随后被新相位群落接替。'),
        ),
        interactions=(
            ('temporal_windows', '需以多源时标核对可进入、可返回和因果稳定的窗口。'),
            ('phase_anchors', '星都残柱、质量节点和记录装置可固定局部相位。'),
            ('provenance_verification', '相似器物可能来自不同时间线，必须核对结构化来源而非名称。'),
        ),
    ),
    'tower_of_creation': _profile(
        'hidden.tower_of_creation',
        '创世神塔是虚空神殿核心由原初光、造物层级与回收型看护者共同维持的生成设施。',
        drivers=(
            ('primordial_light', '原初光按塔层分化为物质、生命与规则的生成条件。'),
            ('creation_strata', '各层承担原料组合、原型培育、校准和淘汰等不同职能。'),
            ('caretaker_constructs', '看护构造物依照版本和权限维护、回收或隔离造物。'),
        ),
        resources=(
            ('creation_dust_family', '生成过程沉降的净尘、基质微粒和未定型物质形成的原料谱系。'),
            ('prototype_biological', '被淘汰或自然蜕落的原型组织、种子、外壳与培养介质谱系。'),
            ('calibration_artifact', '测量尺、规则刻片、容器和看护者维修件形成的校准材料谱系。'),
        ),
        evolutions=(
            ('design_iteration', '一种原型结束后，塔层会调整参数并培育不同后继版本。'),
            ('floor_reconfiguration', '职能过载或故障会让造物层合并、隔离或交换位置。'),
            ('creation_reclamation', '不稳定造物被回收为基质，原料随后进入其他层的生成循环。'),
        ),
        interactions=(
            ('blueprint_permissions', '规则刻片、版本号和职责印记决定可操作的造物设施。'),
            ('creator_caretaker_relations', '修复、证明用途或绕过看护者会导致不同的设施响应。'),
            ('ascending_strata', '原料层、培育层、校准层和顶层光源具有不同因果关系。'),
        ),
    ),
    'wax_sealed_penitence_hall': _profile(
        'hidden.wax_sealed_penitence_hall',
        '蜡封忏悔堂是废弃修道院封闭侧廊中由蜡潮、沉积证词与守蜡生物共同维持的密封堂区。',
        drivers=(
            ('wax_tide', '地下热流推动旧蜡熔化、涌动与凝结，通道随蜡位而开闭。'),
            ('deposited_testimony', '曾经说出的忏悔在蜡层中留下带来源的声音和刻痕，所述内容仍需证据核对。'),
            ('wax_dwellers', '以蜡质、灯灰和湿壁为食的群落改变熔蜡分布与封层强度。'),
        ),
        resources=(
            ('seal_wax', '经历反复热循环的封蜡、夹层树脂与残响凝结物形成的密封材料谱系。'),
            ('wick_fiber', '旧灯芯、包蜡纤维与守蜡生物筑巢介质形成的耐热纤维谱系。'),
            ('thermal_crust', '蜡灰、壁盐与热孔沉积形成的分层矿壳谱系。'),
        ),
        evolutions=(
            ('channel_closure', '蜡道堵塞使热流改道，在保留旧封存痕迹的同时形成新的熔融边界。'),
            ('layer_exposure', '侵蚀与剥落会暴露不同年代的蜡层，已经毁损的证词不会重新生成。'),
            ('colony_turnover', '热湿条件改变后，原守蜡群落退出，其留下的巢壁由耐受不同的群落利用。'),
        ),
        interactions=(
            ('wax_phase', '温度、蜡面纹理与滴落节律为移动和工具接触提供可观察的时机。'),
            ('stratum_provenance', '封层先后、声源位置与保管痕迹共同界定证词的来源和可信范围。'),
            ('heat_flow', '引导或阻断热流会改变相邻蜡道的压力与封闭状态，并留下持续后果。'),
        ),
    ),
    'living_fungal_lung': _profile(
        'hidden.living_fungal_lung',
        '活体菌肺是剧毒钟乳洞深层由收缩菌膜、气压交换与孢子食物网维持的呼吸洞腔。',
        drivers=(
            ('membrane_breath', '附着洞壁的菌膜周期收缩，吸入和排出气体时改变窄道截面。'),
            ('spore_pressure', '孢子随气流在相邻腔室搬运，浓度与毒性暴露随通风条件变化。'),
            ('lung_symbiosis', '滤食生物与腐殖菌共同消耗孢子和残骸，维持不均匀的清洁气隙。'),
        ),
        resources=(
            ('pressure_membrane', '菌囊、弹性隔膜与支撑菌索形成的承压生物材料谱系。'),
            ('spore_sediment', '不同通风阶段沉降的休眠孢子、代谢盐与残留滤质形成的沉积谱系。'),
            ('filter_shed', '滤食生物换落的纤毛、筛板与附生组织形成的过滤材料谱系。'),
        ),
        evolutions=(
            ('airway_obstruction', '堵塞的气道使呼吸负荷转移到旁腔，局部鼓胀或萎缩保留受损痕迹。'),
            ('fungal_generation', '旧菌膜死亡后由不同耐压的菌群接替，生长依赖剩余营养与水分。'),
            ('filter_migration', '过滤群落随孢子流迁移，旧气隙失去维护后会重新积聚悬浮物。'),
        ),
        interactions=(
            ('breath_timing', '气流方向、膜壁松紧与水雾变化使当前呼吸阶段可被辨认。'),
            ('pressure_equalization', '通风、隔离与支撑措施改变局部压差，也会影响相邻腔室。'),
            ('colony_response', '生物群落对光、气流和营养扰动作出各自反应，反应可用于判断其生态需求。'),
        ),
    ),
    'bonewheel_siege_factory': _profile(
        'hidden.bonewheel_siege_factory',
        '骨轮攻城厂是食人魔要塞地下由骨质传动、皮囊风压与废料回收维持的旧攻城器械工场。',
        drivers=(
            ('bone_transmission', '骨轮、齿条与碾压带相互传动，局部负载改变整条机械链的速度。'),
            ('bellows_network', '皮囊风箱把炉火与残余动力输送到各工位，泄漏形成失压和热气区。'),
            ('workshop_scavengers', '工场占据者与食腐生物围绕骨料、油脂和可拆构件形成竞争。'),
        ),
        resources=(
            ('load_bone', '长期承载的轴骨、磨损齿片与压实骨髓形成的结构材料谱系。'),
            ('bellows_hide', '风箱皮膜、密封筋束与输气软管形成的柔性机械材料谱系。'),
            ('machining_residue', '骨粉、润滑脂与金属嵌件磨屑形成的加工副产物谱系。'),
        ),
        evolutions=(
            ('drive_reroute', '轴系损坏或工位停转后，剩余传动绕行并改变其它工位负担。'),
            ('scrap_reworking', '废弃机具被逐步拆分再加工，原器械的完整形态随耗损而消失。'),
            ('crew_succession', '新占据者根据其体型与技术改造工位，保留可辨认的前任加工痕迹。'),
        ),
        interactions=(
            ('motion_reading', '齿轮相位、风箱鼓缩与传送带负载提供通行和操作的时间线索。'),
            ('load_intervention', '支撑、卸载或阻断传动会将机械应力转移到能够追溯的部位。'),
            ('worksite_claims', '工具归属、维修痕迹与补给关系使占据者的工位权限可以核对。'),
        ),
    ),
    'self_rewriting_quarter': _profile(
        'hidden.self_rewriting_quarter',
        '自我改写街区是遗忘废墟内由门槛换接、标记消蚀与迁居群落构成的局部重排城区；重排不改变人物记忆与已经发生的历史。',
        drivers=(
            ('threshold_relinking', '越过特定门槛会触发相邻街段的连接换接，改变可达路径而非改写历史。'),
            ('surface_erasure', '重排时的界面摩擦消蚀现场新旧标记，保存在人物记忆或稳定物体内的记录仍保持连续。'),
            ('mobile_residents', '适应门槛变化的居民与构造生物围绕稳定地块迁居并建立临时通行关系。'),
        ),
        resources=(
            ('junction_condensate', '街段换接边界留下的界核、错层矿膜与磨蚀介质形成的空间界面谱系。'),
            ('foundation_salvage', '不随连接换位消失的地基件、墙芯与旧管网残段形成的建筑材料谱系。'),
            ('resident_wayfinding', '迁居群落制作的测距构件、携带式记录介质与定位器具形成的工具谱系。'),
        ),
        evolutions=(
            ('connection_drift', '磨损与地基沉降使连接规律逐步偏移，既有破坏继续限制可用街段。'),
            ('stable_island_shift', '相对稳定的地块随支撑条件改变而收缩或转移，迁居者留下可追溯的迁移链。'),
            ('marking_adaptation', '使用者不断改变保存记录的介质与方法，旧的记录失败仍有物理原因。'),
        ),
        interactions=(
            ('threshold_comparison', '门框位置、连接次序与携带记录之间的比较能够揭示局部换接规律。'),
            ('route_commitment', '跨越门槛会真实改变队伍所在街段和返程条件，分队与联络方式影响风险。'),
            ('resident_knowledge', '居民掌握的路径经验受其活动范围限制，可以交易或核对而非天然全知。'),
        ),
    ),
    'glass_storm_stratum': _profile(
        'hidden.glass_storm_stratum',
        '玻璃风暴层是熔岩火山气道上方由熔滴急冷、玻璃暴雨与交错风压构成的破碎岩层。',
        drivers=(
            ('quench_front', '熔融岩浆接触冷气后崩裂成玻璃滴与薄片，冷却界面不断迁移。'),
            ('shear_winds', '不同温度的气流相互切变，携带碎片形成方向和强度不同的风暴带。'),
            ('heat_refugia', '耐热生物依附背风岩隙与余温带，利用风暴沉积维持局部食物网。'),
        ),
        resources=(
            ('wind_glass', '经热风拉伸、扭转和淬冷的玻璃晶体形成的脆性矿物谱系。'),
            ('sintered_layers', '反复熔结的粉尘、矿壳与岩隙沉积形成的耐热复合材料谱系。'),
            ('thermal_biota', '耐热生物脱落的隔热壳、纤维与巢膜形成的生物防护谱系。'),
        ),
        evolutions=(
            ('vent_migration', '喷口堵塞和岩层开裂使风压出口迁移，旧安全路线随之失去遮蔽。'),
            ('glass_accumulation', '玻璃碎片堆积后可被再熔或冲走，地形承载条件持续变化。'),
            ('refuge_turnover', '背风岩隙的温度改变促使生物群落迁移或接替，已毁栖地保留熔蚀痕迹。'),
        ),
        interactions=(
            ('storm_observation', '碎片轨迹、气纹与岩面热差使风暴窗口和遮蔽范围能够被判断。'),
            ('shelter_integrity', '支撑、阻风与清障会改变掩体受力，也可能把碎片流导向旁路。'),
            ('thermal_transition', '穿越冷热交界会对已有装备和身体产生符合其材料与能力边界的影响。'),
        ),
    ),
    'dragon_molt_compost': _profile(
        'hidden.dragon_molt_compost',
        '龙蜕腐殖场是巨龙巢穴排弃层中由龙蜕分解、高温菌群与食腐生物维持的腐殖生态。',
        drivers=(
            ('molt_decomposition', '废弃鳞皮与陈旧角质在水分和微生物作用下逐层分解，物质存量有限。'),
            ('thermophilic_fungi', '高温菌群沿热源与富养层扩张，代谢产热改变堆层内部条件。'),
            ('scavenger_web', '食腐生物与其捕食者围绕腐殖堆的熟化阶段竞争和迁移。'),
        ),
        resources=(
            ('colonized_scale', '菌索包覆的鳞壳、角质纤维与矿化表皮形成的复合材料谱系。'),
            ('humic_secretions', '分解产生的腐殖液、菌胶与代谢结晶形成的生化材料谱系。'),
            ('scavenger_casts', '食腐群落蜕下的甲片、口器与巢层残料形成的生物材料谱系。'),
        ),
        evolutions=(
            ('pile_maturation', '新排弃物转为熟化腐殖层，再被消耗或搬离，耗尽的旧龙蜕不复生。'),
            ('heat_pocket_shift', '通气和含水量改变代谢热区，使菌层局部失活并由其它分解者接替。'),
            ('scavenger_succession', '不同熟化阶段吸引不同食腐群落，捕食关系随食物来源改变。'),
        ),
        interactions=(
            ('compost_layers', '堆温、气味与分层色泽使材料分解程度及承重风险可以辨认。'),
            ('ventilation_effects', '翻动、排水与通气会改变腐殖反应和气体释放，影响范围来自真实通路。'),
            ('feeding_boundaries', '食腐者的警戒与攻击取决于食物、巢穴和干扰程度，各群落反应有别。'),
        ),
    ),
    'reverse_tide_hatchery': _profile(
        'hidden.reverse_tide_hatchery',
        '逆潮孵化滩是恶魔岛峭壁内侧由上行黑潮、侵蚀卵囊与攀壁育幼群落维持的垂直潮滩。',
        drivers=(
            ('ascending_tide', '附着峭壁的黑潮沿连续潮脉上行，离开潮脉的水流受当地正常重力影响。'),
            ('substrate_feeding_eggs', '卵囊吸收附着基质并扩大育幼空腔，侵蚀结果真实改变岩壁支撑。'),
            ('cliff_brood_ecology', '亲代、寄生者与捕食者围绕潮高和孵化阶段划分垂直领地。'),
        ),
        resources=(
            ('brood_membrane', '孵化后脱落的卵膜、胶层与附着丝形成的韧性生物材料谱系。'),
            ('tide_precipitates', '上行潮水搬运并析出的盐壳、黑砂与矿物凝胶形成的潮汐沉积谱系。'),
            ('excavated_substrate', '卵囊侵蚀后留下的多孔岩、矿化薄层与空腔残片形成的基质谱系。'),
        ),
        evolutions=(
            ('tide_path_change', '潮脉断裂或堵塞会使上行水流转入其它连续岩缝，旧潮带逐步干涸。'),
            ('brood_hatching', '卵囊完成孵化后留下空腔，由后继生物利用而非反复恢复为同一窝卵。'),
            ('cliff_collapse', '持续侵蚀导致局部岩壁坠落，潮道与育幼领地随后重新分布。'),
        ),
        interactions=(
            ('vertical_tide', '水线、漂浮物和盐痕能够揭示上行潮脉的边界与当前强度。'),
            ('brood_disturbance', '对卵囊、基质或潮水的干预会分别触发育幼、防御和捕食关系。'),
            ('wall_support', '空腔回声与岩面裂纹反映支撑变化，可据此选择攀行或加固方式。'),
        ),
    ),
    'soul_coin_foundry': _profile(
        'hidden.soul_coin_foundry',
        '亡魂铸币厂是幽冥王都封存工区中由游魂与记忆的压铸、移动熔炉和冥界通行凭信维持的铸造设施。',
        drivers=(
            ('memory_feedstock', '有明确来源的游魂与记忆残留进入冶炼流程，压铸会损耗其原有信息与形态。'),
            ('moving_furnaces', '熔炉沿旧轨道迁移，把热量、投料与废渣流引向不同工位。'),
            ('mint_custody', '看守、铸工与待渡者围绕铸料来源、通行凭信和工场权限形成冲突。'),
        ),
        resources=(
            ('soul_slag', '冶炼损耗形成的魂渣、回声夹杂与冷凝灰形成的铸造副产物谱系。'),
            ('mint_tooling', '压模、坩埚衬层、轨道构件与耐冥火介质形成的设施材料谱系。'),
            ('passage_records', '炉批刻记、交接痕迹与失效印模形成的来源记录谱系。'),
        ),
        evolutions=(
            ('furnace_route_change', '轨道磨损和工位停用使炉群改道，废热和弃料区随之迁移。'),
            ('memory_exhaustion', '已耗散的记忆不能再次提取，后续生产取决于另有来源的新铸料。'),
            ('custodian_turnover', '工场控制权转移会改变铸料征集与分配方式，旧债与已发生的损耗继续存在。'),
        ),
        interactions=(
            ('batch_provenance', '炉批、投料轨迹与交接凭据使铸料归属和加工代价能够核对。'),
            ('furnace_passage', '移动炉群的节奏与余热分布形成不同通路和隔离条件。'),
            ('passage_claims', '冥界凭信的效力由现场既存保管者与通行关系决定，不自动成为酒馆账户或可兑货币。'),
        ),
    ),
    'shadowless_lightwell': _profile(
        'hidden.shadowless_lightwell',
        '无影蚀光井是黑日王庭地层中由向下汇聚的光流、无影井壁与反常热分布形成的深井系统。',
        drivers=(
            ('descending_light', '井壁结构将入射光持续导向深处，局部散射不足使阴影与轮廓失去常见形态。'),
            ('thermal_inversion', '吸收和释热发生在不同井层，明亮程度与温度不再按通常关系变化。'),
            ('light_dependent_biota', '适应弱光、余热或光流梯度的群落占据不同井层并相互捕食。'),
        ),
        resources=(
            ('light_absorbing_deposits', '吸光煤晶、暗色矿膜与导光薄层形成的光学沉积谱系。'),
            ('heat_buffer_materials', '在冷热交界循环形成的多孔岩与储热结壳形成的热交换材料谱系。'),
            ('well_fauna_shed', '井壁生物脱落的感光膜、触须壳与隔热巢材形成的生物材料谱系。'),
        ),
        evolutions=(
            ('light_conduit_erosion', '导光结构破损使光流泄入侧井，旧井层的明暗与温度随之改变。'),
            ('thermal_reservoir_decay', '积蓄热量被逐渐释放或转移，耗尽热区不会无来源恢复。'),
            ('depth_migration', '受光与温度影响的生物群落沿井壁迁移，留下被弃用的旧栖位。'),
        ),
        interactions=(
            ('gradient_measurement', '光流方向、物体边缘与温度反馈可以分别观察，不能只按明暗判断热害。'),
            ('conduit_intervention', '遮挡、反射或破坏井壁导光结构会改变可追溯的局部光热分配。'),
            ('well_boundaries', '井层边界与通气连接界定异常范围，离开该范围后沿用外部已经成立的物理条件。'),
        ),
    ),
    'weightless_wreckage_ring': _profile(
        'hidden.weightless_wreckage_ring',
        '失重残骸环是陨落星都上方由绕核残骸、分区引力与惯性碰撞维持的破碎环带。',
        drivers=(
            ('orbital_debris', '残骸围绕共同核心运动，轨迹由既有质量、速度和碰撞持续改变。'),
            ('gravity_sectors', '不同区段具有方向可观察的局部引力，过渡带允许短暂失重但保留惯性。'),
            ('anchored_life', '附着残骸的群落与维护构造物围绕稳定锚点、水气和遮蔽争夺栖位。'),
        ),
        resources=(
            ('stellar_shell', '引力交界中风化和剥落的星壳、晶膜与层状矿物形成的空间风化谱系。'),
            ('wreckage_structure', '旧船体、星轨支架与断裂连接件形成的可追溯残骸材料谱系。'),
            ('drifting_growth', '依附碎片生长的膜状群落、纤维束与休眠囊形成的悬浮生物材料谱系。'),
        ),
        evolutions=(
            ('collision_cascade', '残骸碰撞产生的新碎片改变邻近通路，原构件的毁损持续保留。'),
            ('anchor_migration', '稳定锚点随大块残骸的轨道变化迁移，原有连线可能拉断或失去张力。'),
            ('habitat_dispersal', '失去遮蔽或水气的附生群落衰退，后继群落依靠实际迁移和繁殖建立。'),
        ),
        interactions=(
            ('relative_motion', '相对速度、系缆张力与漂浮物轨迹使可接近窗口能够被估计。'),
            ('sector_crossing', '跨越引力分区会改变受力方向，身体和携带物继续遵守已有动量与能力边界。'),
            ('anchor_choice', '锚点、连接方式与借力会真实影响队伍及残骸运动，不预设唯一通行手段。'),
        ),
    ),
    'law_scrap_yard': _profile(
        'hidden.law_scrap_yard',
        '法则废料场是虚空神殿隔离层中由报废规则构件、局部物理失效区与回收设施维持的弃置场；每块地带只承载一种可辨认的物理失效。',
        drivers=(
            ('failed_rule_components', '报废构件各自维持一种局部物理失效，失效性质随该构件身份固定而非临场改换。'),
            ('bounded_failure_zones', '构件周围的异常边界具有可观察的物体反馈，范围由构件与隔离条件决定。'),
            ('reclamation_work', '回收设施和适应局部失效的群落搬运残片，改变废料的空间分布。'),
        ),
        resources=(
            ('rule_shards', '失效构件剥落的刻片、校准介质与破碎铭层形成的规则残材谱系。'),
            ('isolation_structures', '隔离框、边界标识与维修器件形成的约束设施材料谱系。'),
            ('adapted_colony_materials', '适应局部物理差异的生物脱落物与筑巢介质形成的环境适应谱系。'),
        ),
        evolutions=(
            ('component_decay', '构件耗损使其失效区缩退或消散，已经报废的同一构件不会无故恢复。'),
            ('scrap_displacement', '搬运与堆塌移动既有异常边界，但保留每件构件已经建立的失效性质。'),
            ('isolation_repair', '隔离设施的修复或损坏改变异常相邻关系，后果需由对应位置与传递路径解释。'),
        ),
        interactions=(
            ('boundary_observation', '借助既有物体反馈可以分辨局部物理差异及边界，不预设角色必须使用哪种能力。'),
            ('controlled_contact', '接触和搬移构件会改变其附近条件，持有材料本身不授权任意改写其它规则。'),
            ('isolation_provenance', '校准记录、构件身份与修补痕迹共同限定已知异常；人物记忆、生命结果与世界历史保持连续。'),
        ),
    ),
}


def get_dungeon_ecology_profile(source_dungeon: str) -> dict | None:
    """返回普通副本的画像副本，调用方不能修改模块权威定义。"""
    profile = DUNGEON_ECOLOGY_PROFILES.get(str(source_dungeon or '').strip())
    return deepcopy(profile) if profile else None


def _build_hidden_profile(hidden: Mapping, parent_profile: Mapping) -> dict:
    code = str(hidden.get('code') or '').strip()
    source = str(hidden.get('source_dungeon') or '').strip()
    canonical_parent = DUNGEON_ECOLOGY_PROFILES.get(source)
    if (
        not canonical_parent
        or not isinstance(parent_profile, Mapping)
        or parent_profile.get('profile_id') != canonical_parent.get('profile_id')
    ):
        raise ValueError(f'隐藏副本 {code or "<empty>"} 的父层生态画像不一致')

    profile = HIDDEN_DUNGEON_ECOLOGY_PROFILES.get(code)
    if not profile:
        raise ValueError(f'未配置的 S1 隐藏副本生态画像：{code or "<empty>"}')
    if profile.get('profile_id') != f'hidden.{code}':
        raise ValueError(f'隐藏副本 {code} 的生态画像 ID 与稳定 code 不一致')
    return deepcopy(profile)


def _resolve_profile_and_layer(
    target_dungeon: str,
    *,
    source_dungeon: str | None = None,
    hidden_dungeon_code: str | None = None,
    hidden_dungeon_context: Mapping | None = None,
) -> tuple[dict, dict]:
    target = str(target_dungeon or '').strip()
    context = hidden_dungeon_context if isinstance(hidden_dungeon_context, Mapping) else {}
    context_code = str(context.get('code') or '').strip()
    context_source = str(context.get('source_dungeon') or '').strip()
    context_target = str(context.get('dungeon') or '').strip()
    code = str(hidden_dungeon_code or context_code or '').strip()

    hidden = get_s1_hidden_dungeon_by_code(code) if code else get_s1_hidden_dungeon(target)
    if hidden:
        hidden_code = str(hidden.get('code') or '').strip()
        hidden_source = str(hidden.get('source_dungeon') or '').strip()
        hidden_name = str(hidden.get('name') or '').strip()
        if target != hidden_name:
            raise ValueError('隐藏副本实际目标与稳定配置不一致')
        if context_target and context_target != hidden_name:
            raise ValueError('隐藏副本上下文目标与稳定配置不一致')
        if context_source and context_source != hidden_source:
            raise ValueError('隐藏副本上下文来源与稳定配置不一致')
        if source_dungeon and str(source_dungeon).strip() != hidden_source:
            raise ValueError('隐藏副本来源与普通副本不一致')
        parent = DUNGEON_ECOLOGY_PROFILES.get(hidden_source)
        if not parent:
            raise ValueError(f'隐藏副本来源缺少生态画像：{hidden_source}')
        return _build_hidden_profile(hidden, parent), {
            'kind': 'hidden',
            'ref': hidden_code,
            'display_name': hidden_name,
            'source_dungeon': hidden_source,
        }

    source = str(source_dungeon or target).strip()
    if source != target:
        raise ValueError('普通副本的实际目标必须等于来源副本')
    profile = DUNGEON_ECOLOGY_PROFILES.get(source)
    if not profile:
        raise ValueError(f'未配置的 S1 普通副本生态画像：{source or "<empty>"}')
    return profile, {
        'kind': 'base',
        'ref': 'base',
        'display_name': source,
        'source_dungeon': source,
    }


def _profile_candidates(profile: Mapping) -> tuple[dict, ...]:
    return tuple({
        'driver': driver,
        'resource_lineage': resource,
        'evolution_mode': evolution,
        'interaction_mode': interaction,
    } for driver, resource, evolution, interaction in product(
        profile['drivers'],
        profile['resource_lineages'],
        profile['evolution_modes'],
        profile['interaction_modes'],
    ))


def _motif_fingerprint(profile_id: str, candidate: Mapping) -> str:
    return (
        f'v1|{profile_id}'
        f'|driver={candidate["driver"]["ref"]}'
        f'|resource={candidate["resource_lineage"]["ref"]}'
        f'|evolution={candidate["evolution_mode"]["ref"]}'
        f'|interaction={candidate["interaction_mode"]["ref"]}'
    )


def _build_snapshot(profile: Mapping, layer: Mapping, candidate: Mapping) -> dict:
    profile_id = str(profile['profile_id'])
    return {
        'schema_version': ECOLOGY_IDENTITY_SCHEMA_VERSION,
        'profile_revision': ECOLOGY_IDENTITY_PROFILE_REVISION,
        'profile_id': profile_id,
        'target_dungeon': str(layer['display_name']),
        'layer': {
            'kind': str(layer['kind']),
            'ref': str(layer['ref']),
            'display_name': str(layer['display_name']),
            'source_dungeon': str(layer['source_dungeon']),
        },
        'identity_anchor': str(profile['identity_anchor']),
        **{field: {
            'ref': str(candidate[field]['ref']),
            'description': str(candidate[field]['description']),
        } for field in _COMPONENT_FIELDS},
        'motif_fingerprint': _motif_fingerprint(profile_id, candidate),
        'source_policy': {
            'profile_refs_are_families': True,
            'finite_source_reuse_allowed': False,
        },
    }


def project_ecology_identity_snapshot(
    value,
    *,
    expected_target_dungeon: str | None = None,
    expected_source_dungeon: str | None = None,
) -> dict | None:
    """逐层白名单校验并投影一个已冻结快照；无效值返回 ``None``。"""
    if not isinstance(value, Mapping):
        return None
    if type(value.get('schema_version')) is not int or value.get('schema_version') != ECOLOGY_IDENTITY_SCHEMA_VERSION:
        return None
    if type(value.get('profile_revision')) is not int or value.get('profile_revision') != ECOLOGY_IDENTITY_PROFILE_REVISION:
        return None

    target = value.get('target_dungeon')
    layer_value = value.get('layer')
    if not isinstance(target, str) or not target.strip() or not isinstance(layer_value, Mapping):
        return None
    target = target.strip()
    if expected_target_dungeon is not None and target != str(expected_target_dungeon or '').strip():
        return None

    kind = layer_value.get('kind')
    ref = layer_value.get('ref')
    display_name = layer_value.get('display_name')
    source = layer_value.get('source_dungeon')
    if not all(isinstance(item, str) and item.strip() for item in (kind, ref, display_name, source)):
        return None
    if display_name.strip() != target:
        return None
    if (
        expected_source_dungeon is not None
        and source.strip() != str(expected_source_dungeon or '').strip()
    ):
        return None
    try:
        if kind.strip() == 'hidden':
            profile, layer = _resolve_profile_and_layer(
                target,
                source_dungeon=source.strip(),
                hidden_dungeon_code=ref.strip(),
            )
        elif kind.strip() == 'base' and ref.strip() == 'base':
            profile, layer = _resolve_profile_and_layer(target, source_dungeon=source.strip())
        else:
            return None
    except ValueError:
        return None

    profile_id = value.get('profile_id')
    identity_anchor = value.get('identity_anchor')
    if profile_id != profile['profile_id'] or identity_anchor != profile['identity_anchor']:
        return None
    if any(layer_value.get(key) != layer[key] for key in ('kind', 'ref', 'display_name', 'source_dungeon')):
        return None

    available = {
        'driver': {row['ref']: row for row in profile['drivers']},
        'resource_lineage': {row['ref']: row for row in profile['resource_lineages']},
        'evolution_mode': {row['ref']: row for row in profile['evolution_modes']},
        'interaction_mode': {row['ref']: row for row in profile['interaction_modes']},
    }
    selected = {}
    for field in _COMPONENT_FIELDS:
        item = value.get(field)
        if not isinstance(item, Mapping):
            return None
        item_ref = item.get('ref')
        canonical = available[field].get(item_ref) if isinstance(item_ref, str) else None
        if not canonical or item.get('description') != canonical['description']:
            return None
        selected[field] = canonical

    expected_fingerprint = _motif_fingerprint(str(profile_id), selected)
    if value.get('motif_fingerprint') != expected_fingerprint:
        return None
    source_policy = value.get('source_policy')
    if not isinstance(source_policy, Mapping):
        return None
    if source_policy.get('profile_refs_are_families') is not True:
        return None
    if source_policy.get('finite_source_reuse_allowed') is not False:
        return None
    return _build_snapshot(profile, layer, selected)


def select_dungeon_ecology_identity_snapshot(
    target_dungeon: str,
    *,
    source_dungeon: str | None = None,
    hidden_dungeon_code: str | None = None,
    seed: object = '',
    recent_snapshots: Iterable[Mapping] = (),
    max_consecutive_motif: int = 2,
) -> dict:
    """从 81 种组合中确定性选择最少使用的生态身份快照。

    ``recent_snapshots`` 必须按 newest-first 提供。只消费可通过权威投影且与当前
    profile 相同的结构化快照；文本、物品名和其他副本都不会影响选择。
    """
    profile, layer = _resolve_profile_and_layer(
        target_dungeon,
        source_dungeon=source_dungeon,
        hidden_dungeon_code=hidden_dungeon_code,
    )
    projected_history = []
    for raw in recent_snapshots or ():
        projected = project_ecology_identity_snapshot(raw)
        if projected and projected['profile_id'] == profile['profile_id']:
            projected_history.append(projected)

    fingerprint_counts = Counter(row['motif_fingerprint'] for row in projected_history)
    component_counts = {
        field: Counter(row[field]['ref'] for row in projected_history)
        for field in _COMPONENT_FIELDS
    }
    blocked_fingerprint = None
    maximum_streak = max(1, int(max_consecutive_motif or 1))
    if projected_history:
        newest_fingerprint = projected_history[0]['motif_fingerprint']
        newest_streak = 0
        for row in projected_history:
            if row['motif_fingerprint'] != newest_fingerprint:
                break
            newest_streak += 1
        if newest_streak >= maximum_streak:
            blocked_fingerprint = newest_fingerprint

    candidates = _profile_candidates(profile)

    def score(candidate: Mapping) -> tuple:
        fingerprint = _motif_fingerprint(str(profile['profile_id']), candidate)
        counts = [component_counts[field][candidate[field]['ref']] for field in _COMPONENT_FIELDS]
        tie_breaker = sha256(
            f'{profile["profile_id"]}|{layer["ref"]}|{seed}|{fingerprint}'.encode('utf-8')
        ).hexdigest()
        return (
            1 if fingerprint == blocked_fingerprint else 0,
            fingerprint_counts[fingerprint],
            max(counts),
            sum(counts),
            tie_breaker,
        )

    selected = min(candidates, key=score)
    return _build_snapshot(profile, layer, selected)


def select_ecology_identity_snapshot(
    target_dungeon: str,
    *,
    expedition_log_id,
    recent_snapshots: Iterable[Mapping] = (),
    hidden_dungeon_context: Mapping | None = None,
    max_consecutive_motif: int = 2,
) -> dict:
    """Runner 友好包装：从冻结的隐藏副本上下文推导父层与稳定 code。"""
    context = hidden_dungeon_context if isinstance(hidden_dungeon_context, Mapping) else None
    context_target = str(context.get('dungeon') or '').strip() if context else ''
    if context_target and context_target != str(target_dungeon or '').strip():
        raise ValueError('隐藏副本上下文目标与实际目标不一致')
    return select_dungeon_ecology_identity_snapshot(
        target_dungeon,
        source_dungeon=(str(context.get('source_dungeon') or '').strip() if context else None),
        hidden_dungeon_code=(str(context.get('code') or '').strip() if context else None),
        seed=expedition_log_id,
        recent_snapshots=recent_snapshots,
        max_consecutive_motif=max_consecutive_motif,
    )


def build_ecology_identity_review_context(snapshot) -> dict | None:
    """构造 Observer 只读所需的最小身份合同，不暴露画像的其他字段。"""
    projected = project_ecology_identity_snapshot(snapshot)
    if not projected:
        return None
    return {
        'profile_id': projected['profile_id'],
        'profile_revision': projected['profile_revision'],
        'motif_fingerprint': projected['motif_fingerprint'],
        'expected_ref_keys': [
            projected[field]['ref'] for field in _COMPONENT_FIELDS
        ],
    }
