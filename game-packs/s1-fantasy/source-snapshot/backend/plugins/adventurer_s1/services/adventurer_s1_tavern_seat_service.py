"""深渊酒馆 S1 大堂座位与点酒服务。

玩家把自己空闲（resting/idle）的冒险者安排到酒馆座位上，并掏冒险者自己的
金币点酒。点酒纯娱乐：没有任何数值效果，只按酒的档次随机抽一条神态文案
摆在座位上，所有玩家可见。

设计要点：
- 座位固定布局由 seed 写入（吧台单人座 + 多人桌），状态记录在行上
- 入座不改变冒险者 status：坐着喝酒仍算 resting 回精力
- 被组队 / 探险 / 建造拉走后由每分钟 scheduler 清座，list_seats 只读投影
- 抢座用原子 UPDATE ... WHERE adventurer_id IS NULL，防并发双坐
- 扣金币一律走 S1CurrencyService（行锁 + 流水日志），余额不足点不了
"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_database_result, player_operation_failure,
)
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app, has_app_context
from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1TavernSeat,
    AdventurerS1TavernTreatRequest,
)
from utils.time_utils import to_utc_iso

# ── 酒单元数据（仿 MATERIAL_META 模式：数据定义放本服务文件） ──────────────
#
# 27 款，价格从低到高（2~500），一律整数金币（用户拍板：价格不要小数）。
# 列表必须保持按 price 升序排列（菜单接口按定义顺序外发）。
# texts 是「喝酒神态」静态文案池，点酒时随机抽一条存到座位上（不调 AI，
# 零 token 消耗）。文案为玩家可见叙事文本，禁止系统化术语。
# 接口只外发 key/name/icon/price/desc，文案池本体不出接口。
S1_TAVERN_DRINKS = [
    {
        'key': 'orange_juice',
        'name': '鲜榨橙汁',
        'icon': '🍊',
        'price': 2,
        'desc': '现摘现榨的甜橙汁，一滴酒精都没有，清醒者的骄傲。',
        'texts': [
            '吸溜一大口，酸得眯起一只眼，又甜得翘起嘴角。',
            '端着橙汁朝举杯的酒客们回敬，气势丝毫不输。',
            '认真地用小勺捞杯底的果肉，一脸专注。',
            '喝完舔了舔嘴唇，郑重宣布今天也要保持清醒。',
            '被邻座笑话喝果汁，挺直腰板说强者不需要酒精。',
        ],
    },
    {
        'key': 'barley_ale',
        'name': '麦芽淡啤',
        'icon': '🍺',
        'price': 5,
        'desc': '酒保随手打的一杯淡啤，泡沫比酒多，胜在便宜管够。',
        'texts': [
            '一口闷掉半杯，抹了抹嘴角的泡沫，满足地哈了口气。',
            '捧着杯子小口抿着，偶尔被气泡呛得皱皱鼻子。',
            '盯着杯里的泡沫发呆，像是在数今天又花了几个铜板。',
            '跟邻座碰了碰杯，溅出来的泡沫比喝进嘴里的还多。',
            '喝得太急打了个嗝，不好意思地缩了缩脖子。',
        ],
    },
    {
        'key': 'old_friend_ale',
        'name': '长明空席麦酒',
        'icon': '🕯️',
        'price': 6,
        'desc': '给没能回到酒馆的老队友留一杯，也给还坐在这里的人压一压心事。酒保总会把价钱算得便宜些。',
        'texts': [
            '把第一口缓缓洒在地上，低声念了一个很久没人提起的名字。',
            '在对面的空位前也摆了一只杯子，独自把两份往事喝完。',
            '喝得比平时快了些，红着眼眶笑骂老队友还欠自己一顿酒。',
            '用指节轻敲杯沿，像从前出发前那样，轻声说了句「归队」。',
            '举杯朝旧队伍常坐的角落遥遥一敬，沉默许久才仰头喝下。',
            '一边喝一边讲起旧日糗事，讲到最后，笑声和叹息混在了一起。',
        ],
    },
    {
        'key': 'apple_cider',
        'name': '苹果西打',
        'icon': '🍎',
        'price': 8,
        'desc': '农家苹果榨汁发酵的微甜果酒，气泡清爽，不胜酒力的人也能来一杯。',
        'texts': [
            '被气泡顶得眯起眼，咂咂嘴说这哪算酒，分明是果汁。',
            '捧着杯子咕咚咕咚，喝出了苹果园的香气。',
            '小小打了个嗝，脸上浮起一点苹果色的红。',
            '用舌尖追着杯沿的泡沫，一脸意犹未尽。',
            '笑着说这味道像家乡的秋天，又续了一大口。',
        ],
    },
    {
        'key': 'fallen_sakura_sake',
        'name': '落樱归途酒',
        'icon': '🌸',
        'price': 9,
        'desc': '以盐渍樱花浸出的清甜酒，花瓣沉在杯底，替没能同行归来的人走完最后一程。',
        'texts': [
            '拈起杯中花瓣看了许久，轻声说这次换自己替老队友看春天。',
            '把酒沿桌边缓缓洒下一线，像为那段没能走完的归途引路。',
            '望着花瓣在杯底打旋，念起当年并肩冲进副本时喊过的口号。',
            '只抿了一小口便合上眼，直到酒馆的喧闹重新漫进耳中。',
            '将一片樱花留在空杯旁，说等下次凯旋再替那个人续杯。',
            '举杯朝门外遥遥一敬，笑着说老队友若在，一定嫌这酒太甜。',
        ],
    },
    {
        'key': 'honey_goat_milk',
        'name': '蜂蜜热羊奶',
        'icon': '🥛',
        'price': 10,
        'desc': '温热的山羊奶冲一勺蜂蜜，暖胃安神，深夜抵达的旅人最爱点它。',
        'texts': [
            '捧着杯子暖手，奶白的热气糊了满脸。',
            '喝出一圈奶胡子，自己浑然不觉。',
            '小口小口地啜，眼皮渐渐开始打架。',
            '满足地叹了口气，说这才是过日子的味道。',
            '把最后一口喝得干干净净，连杯底都不剩。',
        ],
    },
    {
        'key': 'lemon_fizz',
        'name': '柠檬气泡水',
        'icon': '🍋',
        'price': 12,
        'desc': '冰镇井水兑柠檬汁打出细密气泡，专治探险归来的一身燥热。',
        'texts': [
            '气泡冲上鼻尖，打了个小小的喷嚏。',
            '贴着冰凉的杯壁蹭了蹭脸，舒服得眯起眼。',
            '一口气喝下半杯，长长地哈出一口凉气。',
            '搅着杯里的柠檬片，看气泡一串串往上跑。',
            '酸得整张脸皱成一团，却又忍不住喝了第二口。',
        ],
    },
    {
        'key': 'ember_vigil_wine',
        'name': '余烬守夜酒',
        'icon': '🔥',
        'price': 14,
        'desc': '深色浆果与暖香料酿成的守夜酒，敬那些在副本里守住退路、却没能踏出出口的人。',
        'texts': [
            '用掌心护着杯中微热，像从前围着营火替值夜的队友留位置。',
            '喝到最后一口仍没有放下杯子，低声报出了全队曾经的名字。',
            '朝门口举杯停了片刻，仿佛下一秒还会有人推门回来换岗。',
            '将杯底一点暗红酒液留着，说这是替守到最后的人留下的火种。',
            '慢慢讲起那场撤退，讲到有人留下断后时，只沉默地抿了一口。',
            '用杯沿轻碰桌面三次，仍按旧队伍守夜换班的暗号向故人致意。',
        ],
    },
    {
        'key': 'honey_mead',
        'name': '蜂蜜米酒',
        'icon': '🍯',
        'price': 15,
        'desc': '温热的米酒兑了一勺野花蜜，是酒馆里最受欢迎的甜口。',
        'texts': [
            '甜味让人放松，嘴角不自觉翘了起来。',
            '小口小口地抿，像是舍不得这股蜂蜜香。',
            '舔了舔嘴唇，盘算着要不要再来一杯。',
            '捧着温热的杯子，整个人都松弛了下来。',
            '抿了一口眯起眼睛，哼起了不成调的小曲。',
        ],
    },
    {
        'key': 'berry_wine',
        'name': '酸莓果酒',
        'icon': '🍓',
        'price': 18,
        'desc': '林间浆果酿的轻果酒，酸甜里藏一点酒劲，喝急了照样上头。',
        'texts': [
            '抿一口就咂咂嘴，像在数里面放了几种浆果。',
            '喝得嘴唇染上一圈莓红，说话都带着果香。',
            '摇晃着杯子看果肉沉沉浮浮，半天舍不得喝。',
            '酸得缩了下脖子，又被回甘哄得眉开眼笑。',
            '不知不觉见了底，盯着空杯惊讶酒劲怎么上来了。',
        ],
    },
    {
        'key': 'sea_rum',
        'name': '海风朗姆',
        'icon': '⚓',
        'price': 22,
        'desc': '港口水手私酿的朗姆酒，带着一股咸咸的海风味，喝多了走路打晃。',
        'texts': [
            '学水手把酒倒进喉咙，咳出一句不成调的船歌。',
            '咂出一丝咸味，恍惚听见了海鸥的叫声。',
            '拍着桌子讲起根本没去过的远洋冒险。',
            '眯眼晃着杯子，像在甲板上找平衡。',
            '喝到兴起，朝酒保喊了一声「升帆」。',
        ],
    },
    {
        'key': 'mulled_wine',
        'name': '香料热红酒',
        'icon': '☕',
        'price': 25,
        'desc': '加了肉桂与橙皮一起温的红酒，捧在手里先暖一半，入喉再暖另一半。',
        'texts': [
            '双手捧着热气腾腾的杯子，先深深闻了一口才舍得喝。',
            '一口下去从喉咙暖到指尖，肩膀都松了下来。',
            '被肉桂香熏得眯起眼，整个人陷进椅背里。',
            '对着杯口轻轻吹气，小口啜饮，像在喝一段温柔的回忆。',
            '喝到一半解开了领口，额角冒出细细的汗珠。',
        ],
    },
    {
        'key': 'hunter_shochu',
        'name': '猎人烧酒',
        'icon': '🏹',
        'price': 30,
        'desc': '猎人酒壶里装的那种粗粝烧酒，入喉辛辣，专为野外的寒夜准备。',
        'texts': [
            '仰头一口闷，哈出的气都带着辣味。',
            '被呛得直瞪眼，缓过来却咧嘴一笑。',
            '抿一口咬一下腮帮，像在嚼这股冲劲。',
            '拧着眉头连灌两口，活像在跟酒较劲。',
            '喝完拍拍胸口，说这酒够劲，配得上风雪。',
        ],
    },
    {
        'key': 'rye_spirit',
        'name': '黑麦烈酒',
        'icon': '🥃',
        'price': 40,
        'desc': '在橡木桶里躺过三年的烈酒，入喉像吞了一团火。',
        'texts': [
            '一口下去呛得直拍桌子，缓过来却又给自己满上一杯。',
            '喉咙里像烧了一把火，表情却越来越痛快。',
            '晃着杯底琥珀色的酒液，眼神有点发直。',
            '灌下一大口，重重把杯子砸回桌面，发出畅快的低吼。',
            '脸颊泛红，开始跟空气比划着什么招式。',
        ],
    },
    {
        'key': 'mercenary_vodka',
        'name': '佣兵伏特加',
        'icon': '⚔️',
        'price': 50,
        'desc': '佣兵团庆功宴的标配烈酒，透明如水、烈如刀锋，一杯下去话匣子全开。',
        'texts': [
            '干杯前先朝空气敬了一下，像在敬没回来的同伴。',
            '一口闷完倒扣酒杯，眼神亮得吓人。',
            '辣得倒吸一口凉气，嗓门却大了一倍。',
            '开始挨桌讲当年的战绩，越讲越离谱。',
            '攥着杯子的手背青筋微鼓，喝得极其郑重。',
        ],
    },
    {
        'key': 'dwarf_ale',
        'name': '矮人窖藏黑啤',
        'icon': '🍻',
        'price': 65,
        'desc': '按矮人古法在山岩酒窖里陈放的黑啤，浓得能挂住杯壁，号称一杯顶三杯。',
        'texts': [
            '学着矮人的样子整杯举过头顶，吼了一嗓子才开喝。',
            '浓稠的酒液挂了满嘴泡沫，抹也不抹，接着灌第二口。',
            '喝得直拍大腿，说这才叫酒，之前喝的都是水。',
            '杯子见底时重重砸在桌上，震得邻座的杯子都跳了一下。',
            '打了个响亮的嗝，不好意思地朝四周拱了拱手。',
        ],
    },
    {
        'key': 'sand_lizard_tequila',
        'name': '沙蜥龙舌兰',
        'icon': '🦎',
        'price': 80,
        'desc': '沙漠商队带来的稀罕烈酒，瓶底泡着一条小沙蜥，据说那才是精华。',
        'texts': [
            '盯着瓶底的小沙蜥犹豫了半天，还是闭眼喝了。',
            '辣得跳起来转了个圈，引来满堂哄笑。',
            '咂咂嘴说有股阳光烤过沙子的味道。',
            '学商队的喝法舔了点盐再灌酒，呛出了眼泪。',
            '喝完盯着空杯发誓再也不喝，眼神毫无说服力。',
        ],
    },
    {
        'key': 'amber_reserve',
        'name': '琥珀陈酿',
        'icon': '🍷',
        'price': 100,
        'desc': '酒窖深处的陈年好酒，据说只有酒保心情好才舍得开封。',
        'texts': [
            '先闻了闻杯口，才慢条斯理地抿了一小口，一脸行家做派。',
            '让酒液在杯中转了两圈，对着光眯眼欣赏它的色泽。',
            '每喝一口都要回味半天，像是在品鉴一段往事。',
            '靠在椅背上慢慢啜饮，浑身写着「别来打扰」。',
            '难得露出温和的笑意，连坐姿都优雅了几分。',
        ],
    },
    {
        'key': 'moonlight_brandy',
        'name': '月光白兰地',
        'icon': '🌙',
        'price': 120,
        'desc': '只在月圆夜蒸馏的白兰地，杯中泛着淡淡的银光，安静的人最懂它。',
        'texts': [
            '对着烛光转动杯子，看银光一圈圈荡开。',
            '抿一口闭上眼，像在听很远的地方传来的歌。',
            '难得安静下来，连呼吸都放轻了。',
            '指尖绕着杯口画圈，思绪明显飘去了别处。',
            '微微一笑，把月色和酒一起咽了下去。',
        ],
    },
    {
        'key': 'elf_nectar',
        'name': '精灵花露',
        'icon': '🌸',
        'price': 150,
        'desc': '据说以晨露和林间花蜜酿成的澄澈酒液，香气会随呼吸在舌尖变化三次。',
        'texts': [
            '凑近杯口轻嗅，惊讶地睁大眼睛，又嗅了第二次。',
            '浅浅抿了一口，半天没说话，像是怕惊扰了什么。',
            '闭着眼细品，嘴角的笑意一点点漾开。',
            '对着光看杯中浮动的微光，舍不得一口喝完。',
            '喝完轻轻呼出一口气，整个人像被林间的风洗过一遍。',
        ],
    },
    {
        'key': 'bard_champagne',
        'name': '吟游诗人香槟',
        'icon': '🥂',
        'price': 180,
        'desc': '开瓶声清脆得像一句即兴诗，气泡升起时连酒馆都浪漫了几分。',
        'texts': [
            '被开瓶声惊得一抖，随即跟着鼓起掌来。',
            '举杯环视四周，即兴来了两句不太押韵的诗。',
            '看着气泡一串串往上冒，眼睛亮晶晶的。',
            '跟半个酒馆的人碰了杯，谁也不认识谁。',
            '抿一口眯起眼，说这泡泡在舌尖上跳舞。',
        ],
    },
    {
        'key': 'witch_brew',
        'name': '女巫秘酿',
        'icon': '🧪',
        'price': 220,
        'desc': '颜色每隔一刻钟变一次的神秘调和酒，女巫赌咒发誓说喝不死人。',
        'texts': [
            '盯着杯里的颜色从紫变绿，迟迟不敢下口。',
            '捏着鼻子灌了一口，表情精彩纷呈。',
            '咂咂嘴说尝出了七种味道，每种都说不上来。',
            '头顶仿佛冒出一缕彩色的烟，本人毫无察觉。',
            '喝完对着空杯打了个彩虹色的嗝。',
        ],
    },
    {
        'key': 'dragon_flame',
        'name': '龙息火酒',
        'icon': '🔥',
        'price': 250,
        'desc': '传说兑了一滴龙息的镇店之宝，喝前请先签好遗嘱（开玩笑的）。',
        'texts': [
            '杯口还冒着微微的火星，深吸一口气才敢下嘴。',
            '一口下去整张脸涨得通红，呼出的气都是烫的。',
            '拍着胸口咳了半天，却竖起大拇指表示再来一杯。',
            '眼里燃起两簇小火苗，大声宣布自己天下无敌。',
            '喝完整个人晃了晃，扶着桌子才稳住，咧嘴笑得像打赢了一场仗。',
        ],
    },
    {
        'key': 'frost_icewine',
        'name': '霜语冰酒',
        'icon': '❄️',
        'price': 300,
        'desc': '极北冰原冻在藤上的葡萄压出的琼浆，杯壁永远挂着一层细霜。',
        'texts': [
            '指尖碰到杯壁的霜，惊讶地缩了一下手。',
            '一小口含在嘴里半天，像怕它太快化掉。',
            '呼出的气都带着凉意，眼神却暖洋洋的。',
            '看着杯壁的霜花出神，像想起了某个雪夜。',
            '喝完轻轻打了个寒颤，又满足地裹紧了披风。',
        ],
    },
    {
        'key': 'royal_gold',
        'name': '王室金樽',
        'icon': '👑',
        'price': 350,
        'desc': '据说和王宫宴会同一批窖藏的金色佳酿，端起杯子的人都会不自觉挺直腰。',
        'texts': [
            '端起酒杯的瞬间不自觉挺直了腰板。',
            '小拇指微微翘起，喝相突然讲究了起来。',
            '抿一口环顾四周，仿佛整个酒馆都是自己的领地。',
            '对着金色的酒液照了照自己，满意地点点头。',
            '放下杯子时轻得没有一点声音，贵气拿捏得死死的。',
        ],
    },
    {
        'key': 'phoenix_feather',
        'name': '凤凰羽觞',
        'icon': '🪶',
        'price': 420,
        'desc': '杯口漂着一根永不熄灭的火羽，酒液暖而不烫，喝完像重生了一遍。',
        'texts': [
            '小心翼翼绕开那根火羽下嘴，姿势相当别扭。',
            '一口下去暖流窜遍全身，疲惫像被火燎掉了一层。',
            '看着火羽的光映在酒里，舍不得搅碎它。',
            '喝到一半长舒一口气，眼里的血丝都淡了。',
            '喝完伸了个大大的懒腰，宣布自己焕然一新。',
        ],
    },
    {
        'key': 'abyss_tear',
        'name': '深渊之泪',
        'icon': '🌌',
        'price': 500,
        'desc': '酒保从柜台最深处取出的神秘黑色酒液，传说每一滴都映着深渊的星空。',
        'texts': [
            '盯着杯中缓缓旋转的幽光，迟迟没敢下嘴。',
            '一口入喉，瞳孔里仿佛闪过一片倒悬的星空。',
            '喝完久久不语，最后只憋出一句「值了」。',
            '指尖微微发颤，却又忍不住凑近再看一眼杯底。',
            '放下杯子时眼神变得很远，像是去深渊边上走了一遭。',
        ],
    },
]

S1_TAVERN_DRINK_MAP = {d['key']: d for d in S1_TAVERN_DRINKS}


class AdventurerS1TavernSeatService:
    """酒馆大堂座位：入座 / 离座 / 点酒 / 全景列表。"""

    @staticmethod
    def get_seated_adventurer_ids(adventurer_ids=None, *, session=None) -> set:
        """批量查询当前占座的冒险者 ID。adventurer_ids 为空则返回全表占座集合。"""
        q = AdventurerS1TavernSeat.query.with_entities(
            AdventurerS1TavernSeat.adventurer_id,
        ).filter(AdventurerS1TavernSeat.adventurer_id.isnot(None))
        if session is not None:
            q = q.with_session(session)
        if adventurer_ids is not None:
            ids = list(adventurer_ids)
            if not ids:
                return set()
            q = q.filter(AdventurerS1TavernSeat.adventurer_id.in_(ids))
        return {row[0] for row in q.all()}

    @staticmethod
    def clear_seats_for_adventurer(
        adventurer_id: int,
        *,
        now: Optional[datetime] = None,
        locked_adv: Optional[AdventurerS1] = None,
    ) -> bool:
        """落账酒馆恢复与食物后清座；由外层事务提交。

        状态切换调用方已持有冒险者锁时必须传入 ``locked_adv``，避免在
        ``no_autoflush`` 外层里刷新掉尚未落库的权威状态变更。
        """
        adv = locked_adv
        if adv is not None and int(adv.id) != int(adventurer_id):
            raise ValueError('locked_adv 与 adventurer_id 不一致')
        if adv is None:
            adv = (
                AdventurerS1.query
                .filter(AdventurerS1.id == adventurer_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
        seats = AdventurerS1TavernSeat.query.filter(
            AdventurerS1TavernSeat.adventurer_id == adventurer_id,
        ).order_by(
            AdventurerS1TavernSeat.id.asc(),
        ).populate_existing().with_for_update().all()
        settled_at = now or _portable_utc_now()
        if adv and seats:
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                flush_recovery_to_db,
            )
            flush_recovery_to_db(adv, now=settled_at, tavern_seated=True)
        AdventurerS1TavernSeatService.settle_and_clear_seat_rows(
            seats,
            adv,
            settled_at,
        )
        if adv and seats:
            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.resume_dorm_rest(adv, settled_at)
        return bool(seats)

    @staticmethod
    def settle_and_clear_seat_rows(
        seats: list[AdventurerS1TavernSeat],
        adv: Optional[AdventurerS1],
        now: datetime,
    ) -> dict:
        """结算并清理已由调用方锁定的酒馆座位行。"""
        settlement = (
            {'stamina_restored': 0, 'hp_restored': 0}
            if adv else {}
        )
        for seat in seats:
            if adv:
                current = AdventurerS1TavernSeatService._settle_food(
                    seat,
                    adv,
                    now=now,
                )
                settlement['stamina_restored'] += int(
                    current.get('stamina_restored') or 0
                )
                settlement['hp_restored'] += int(current.get('hp_restored') or 0)
            AdventurerS1TavernSeatService._clear_seat(seat)
        return settlement

    @staticmethod
    def get_drinks_menu() -> Tuple[Optional[list], Optional[str]]:
        """酒单（不含文案池本体）。静态数据，前端可一次拉取后缓存。"""
        menu = [
            {'key': d['key'], 'name': d['name'], 'icon': d['icon'],
             'price': d['price'], 'desc': d['desc']}
            for d in S1_TAVERN_DRINKS
        ]
        return menu, None

    @staticmethod
    def _foods() -> list:
        if has_app_context():
            return list(current_app.config.get('ADVENTURER_S1_TAVERN_FOODS', []))
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin
        return list(AdventurerS1Plugin.default_config.get('ADVENTURER_S1_TAVERN_FOODS', []))

    @staticmethod
    def _food_map() -> dict:
        return {item['key']: item for item in AdventurerS1TavernSeatService._foods()}

    @staticmethod
    def get_foods_menu() -> Tuple[Optional[list], Optional[str]]:
        """Lv1 食物菜单；文案池只留在服务端。"""
        fields = ('key', 'name', 'icon', 'price', 'duration_minutes',
                  'stamina_restore', 'hp_restore', 'desc')
        return [{field: item[field] for field in fields} for item in AdventurerS1TavernSeatService._foods()], None

    @staticmethod
    def _food_brief(seat: AdventurerS1TavernSeat) -> Optional[dict]:
        meta = AdventurerS1TavernSeatService._food_map().get(seat.food_key)
        if not meta or not seat.food_started_at or not seat.food_expires_at:
            return None
        return {
            'key': meta['key'], 'name': meta['name'], 'icon': meta['icon'],
            'price': meta['price'], 'duration_minutes': meta['duration_minutes'],
            'stamina_restore': meta['stamina_restore'], 'hp_restore': meta['hp_restore'],
            'text': seat.food_text or '',
            'progress_texts': list(meta.get('texts') or []),
            'started_at': seat.food_started_at.isoformat() + 'Z',
            'expires_at': seat.food_expires_at.isoformat() + 'Z',
        }

    @staticmethod
    def _clear_food(seat: AdventurerS1TavernSeat) -> None:
        seat.food_key = None
        seat.food_text = None
        seat.food_started_at = None
        seat.food_expires_at = None
        seat.food_settled_at = None

    @staticmethod
    def _clear_seat(seat: AdventurerS1TavernSeat) -> None:
        seat.adventurer_id = None
        seat.seated_at = None
        seat.drink_key = None
        seat.drink_text = None
        seat.ordered_at = None
        AdventurerS1TavernSeatService._clear_food(seat)

    @staticmethod
    def _recovery_snapshot(adv: AdventurerS1) -> dict:
        """Return the authoritative recovery state used by the client after a seat change."""
        return {
            'adventurer_id': adv.id,
            'stamina': int(adv.stamina or 0),
            'stamina_updated_at': to_utc_iso(adv.stamina_updated_at),
            'hp': int(adv.hp or 0),
            'hp_updated_at': to_utc_iso(adv.hp_updated_at),
            'dorm_recovery_paused': bool(adv._is_dorm_recovery_suspended()),
        }

    @staticmethod
    def _settle_food(seat: AdventurerS1TavernSeat, adv: AdventurerS1, now: Optional[datetime] = None) -> dict:
        """按已用时长一次结算食物；调用方负责持锁和提交。"""
        meta = AdventurerS1TavernSeatService._food_map().get(seat.food_key)
        if not meta or not seat.food_started_at or seat.food_settled_at:
            return {'stamina_restored': 0, 'hp_restored': 0}
        settle_at = min(now or _portable_utc_now(), seat.food_expires_at or _portable_utc_now())
        total_seconds = max(1, int(meta['duration_minutes']) * 60)
        elapsed_seconds = max(0, min(total_seconds, int((settle_at - seat.food_started_at).total_seconds())))
        stamina_reward = int(int(meta.get('stamina_restore', 0)) * elapsed_seconds / total_seconds)
        hp_reward = int(int(meta.get('hp_restore', 0)) * elapsed_seconds / total_seconds)
        max_stamina = int(current_app.config.get('ADVENTURER_S1_MAX_STAMINA', 100))
        old_stamina = max(0, int(adv.stamina or 0))
        old_hp = max(0, int(adv.hp or 0))
        if old_hp <= 0 or adv.status == 'dead':
            hp_reward = 0
        adv.stamina = min(max_stamina, old_stamina + stamina_reward)
        adv.hp = min(max(1, int(adv.max_hp or 1)), old_hp + hp_reward)
        seat.food_settled_at = settle_at
        return {
            'stamina_restored': int(adv.stamina) - old_stamina,
            'hp_restored': int(adv.hp) - old_hp,
        }

    @staticmethod
    def _drink_brief(drink_key: str, drink_text: Optional[str]) -> Optional[dict]:
        """座位上展示的酒信息：酒单元数据 + 该次点酒抽到的文案。"""
        meta = S1_TAVERN_DRINK_MAP.get(drink_key)
        if not meta:
            return None
        return {
            'key': meta['key'],
            'name': meta['name'],
            'icon': meta['icon'],
            'price': meta['price'],
            'text': drink_text or '',
        }

    @staticmethod
    def _drink_menu_item(drink_key: str) -> Optional[dict]:
        meta = S1_TAVERN_DRINK_MAP.get(drink_key)
        if not meta:
            return None
        return {
            'key': meta['key'],
            'name': meta['name'],
            'icon': meta['icon'],
            'price': meta['price'],
            'desc': meta['desc'],
        }

    @staticmethod
    def _emit_tavern_changed(action: str, **payload) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': action,
                'reason': action,
                **payload,
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def list_seats(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """只读返回座位全景；陈旧占座仅在响应中投影为空位。"""
        try:
            seats = AdventurerS1TavernSeat.query.order_by(
                AdventurerS1TavernSeat.sort_order.asc(),
            ).all()

            seated_ids = [s.adventurer_id for s in seats if s.adventurer_id]
            adv_map = {}
            if seated_ids:
                # with_entities 只取标量列，不触发 traits 等子表 selectin 加载
                rows = AdventurerS1.query.with_entities(
                    AdventurerS1.id, AdventurerS1.name, AdventurerS1.status,
                    AdventurerS1.owner_id, AdventurerS1.building_instance_id,
                ).filter(AdventurerS1.id.in_(seated_ids)).all()
                adv_map = {r.id: r for r in rows}

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            stale_seat_ids = []
            for s in seats:
                if not s.adventurer_id:
                    continue
                adv = adv_map.get(s.adventurer_id)
                if adv is None or adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                    stale_seat_ids.append(s.id)

            active_seated_ids = [
                s.adventurer_id for s in seats
                if s.adventurer_id and s.id not in stale_seat_ids
            ]
            incoming_by_seat = {}
            if active_seated_ids:
                pending = (
                    AdventurerS1TavernTreatRequest.query
                    .filter(
                        AdventurerS1TavernTreatRequest.target_user_id == user_id,
                        AdventurerS1TavernTreatRequest.target_adventurer_id.in_(active_seated_ids),
                        AdventurerS1TavernTreatRequest.status == 'pending',
                    )
                    .order_by(AdventurerS1TavernTreatRequest.created_at.desc())
                    .limit(20)
                    .all()
                )
                payer_ids = {r.payer_adventurer_id for r in pending}
                payer_names = {}
                if payer_ids:
                    payer_rows = AdventurerS1.query.with_entities(
                        AdventurerS1.id, AdventurerS1.name,
                    ).filter(AdventurerS1.id.in_(payer_ids)).all()
                    payer_names = {r.id: r.name for r in payer_rows}
                for req in pending:
                    target = adv_map.get(req.target_adventurer_id)
                    incoming_by_seat.setdefault(req.target_seat_id, []).append(
                        req.to_dict(
                            payer_name=payer_names.get(req.payer_adventurer_id),
                            target_name=target.name if target else None,
                            drink=AdventurerS1TavernSeatService._drink_menu_item(req.drink_key),
                        )
                    )

            adv_name_map = {r.id: r.name for r in adv_map.values()}
            from plugins.adventurer_s1.services.adventurer_s1_tavern_chatter_service import (
                AdventurerS1TavernChatterService,
            )
            chatter_by_seat = AdventurerS1TavernChatterService.generate_for_seats(
                seats, adv_name_map, stale_seat_ids,
            )

            result = []
            for s in seats:
                adventurer = None
                drink = None
                food = None
                if s.adventurer_id and s.id not in stale_seat_ids:
                    adv = adv_map.get(s.adventurer_id)
                    adventurer = {
                        'id': adv.id,
                        'name': adv.name,
                        'is_mine': adv.owner_id == user_id,
                    }
                    if s.drink_key:
                        drink = AdventurerS1TavernSeatService._drink_brief(s.drink_key, s.drink_text)
                    if s.food_key:
                        food = AdventurerS1TavernSeatService._food_brief(s)
                seat_data = s.to_dict(adventurer=adventurer, drink=drink, food=food)
                seat_data['incoming_treat_requests'] = incoming_by_seat.get(s.id, [])
                seat_data['chatter'] = chatter_by_seat.get(s.id)
                result.append(seat_data)

            return {'seats': result, 'server_time': _portable_utc_now().isoformat() + 'Z'}, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def sit(user_id: int, seat_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的空闲冒险者入座；已坐在别处则自动换座（清旧座）。"""
        try:
            # 所有实体场所入座统一先锁冒险者行；并发切换场所时以该行串行化，
            # 避免同一冒险者同时占据酒馆、茶舍或温泉座位。
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == adventurer_id,
            ).populate_existing().with_for_update().first()
            if not adv:
                return None, '冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能安排自己的冒险者入座'
            ok, reason = adv.can_dispatch()
            if not ok:
                return None, reason

            seated_at = _portable_utc_now()
            previous_tavern_seats = AdventurerS1TavernSeat.query.filter(
                AdventurerS1TavernSeat.adventurer_id == adventurer_id,
            ).order_by(
                AdventurerS1TavernSeat.id.asc(),
            ).populate_existing().with_for_update().all()

            # 原子抢座：座位必须当前为空
            affected = AdventurerS1TavernSeat.query.filter(
                AdventurerS1TavernSeat.id == seat_id,
                AdventurerS1TavernSeat.adventurer_id.is_(None),
            ).update({
                'adventurer_id': adventurer_id,
                'seated_at': seated_at,
                'drink_key': None,
                'drink_text': None,
                'ordered_at': None,
                'food_key': None,
                'food_text': None,
                'food_started_at': None,
                'food_expires_at': None,
                'food_settled_at': None,
            }, synchronize_session='fetch')
            if not affected:
                seat = AdventurerS1TavernSeat.query.session.get(AdventurerS1TavernSeat, seat_id)
                db_manager.rollback()
                if not seat:
                    return None, '座位不存在'
                return None, '这个座位已经有人了'

            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.switch_rest_source(
                adv,
                'tavern',
                seated_at,
                previous_tavern_seats=previous_tavern_seats,
            )

            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            AdventurerS1StateTransitionCoordinator.invalidate_rest_source_cache()
            return {
                'seat_id': seat_id,
                **AdventurerS1TavernSeatService._recovery_snapshot(adv),
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def leave(user_id: int, seat_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的冒险者离座（连同眼前的酒一起收走）。"""
        try:
            seat = AdventurerS1TavernSeat.query.session.get(AdventurerS1TavernSeat, seat_id)
            if not seat:
                return None, '座位不存在'
            if not seat.adventurer_id:
                return None, '这个座位上没有人'
            adv = (
                AdventurerS1.query
                .filter(AdventurerS1.id == seat.adventurer_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
            if adv is not None and adv.owner_id != user_id:
                return None, '只能叫走自己的冒险者'
            locked_seat = AdventurerS1TavernSeat.query.filter(
                AdventurerS1TavernSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not locked_seat or locked_seat.adventurer_id != seat.adventurer_id:
                return None, '座位状态已经变化，请重试'
            seat = locked_seat

            adv_id = seat.adventurer_id
            settled_at = _portable_utc_now()
            if adv:
                from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                    flush_recovery_to_db,
                )
                flush_recovery_to_db(adv, now=settled_at, tavern_seated=True)
            settlement = AdventurerS1TavernSeatService.settle_and_clear_seat_rows(
                [seat],
                adv,
                settled_at,
            )
            if adv:
                from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                    AdventurerS1StateTransitionCoordinator,
                )
                AdventurerS1StateTransitionCoordinator.resume_dorm_rest(
                    adv,
                    settled_at,
                )

            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err

            recovery_snapshot = (
                AdventurerS1TavernSeatService._recovery_snapshot(adv) if adv else {}
            )
            return {'seat_id': seat_id, **recovery_snapshot, **settlement}, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def order_drink(user_id: int, seat_id: int, drink_key: str) -> Tuple[Optional[dict], Optional[str]]:
        """给座位上自己的冒险者点一杯酒：扣冒险者金币 + 随机抽一条神态文案。

        再点一杯会替换眼前的酒（仍正常扣钱）。没有任何数值效果。
        """
        try:
            meta = S1_TAVERN_DRINK_MAP.get(drink_key)
            if not meta:
                return None, '酒单上没有这种酒'

            seat_ref = AdventurerS1TavernSeat.query.with_entities(
                AdventurerS1TavernSeat.adventurer_id,
            ).filter(AdventurerS1TavernSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '先安排冒险者入座才能点酒'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1TavernSeat.query.filter(
                AdventurerS1TavernSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not seat or seat.adventurer_id != seat_ref.adventurer_id:
                return None, '座位状态已经变化，请重试'
            if not adv:
                return None, '座位上的冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能给自己的冒险者点酒'

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                # 人已离场时只清陈旧占座；不得补发离场后的用餐恢复。
                AdventurerS1TavernSeatService._clear_seat(seat)
                ok, err = player_database_result(db_manager.commit())
                if not ok:
                    return None, err
                return None, '冒险者已经离开酒馆了'

            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            new_balance, err = S1CurrencyService.deduct(
                adv.id, 'gold', float(meta['price']),
                source_type='tavern_drink',
                related_id=seat_id,
                related_type='tavern_seat',
                description=f'酒馆点酒：{meta["name"]}',
                auto_commit=False,
            )
            if err:
                db_manager.rollback()
                if '余额不足' in err:
                    return None, f'金币不足，点不起这杯「{meta["name"]}」'
                return None, err

            seat.drink_key = drink_key
            seat.drink_text = random.choice(meta['texts'])
            seat.ordered_at = _portable_utc_now()
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            return {
                'seat_id': seat_id,
                'drink': AdventurerS1TavernSeatService._drink_brief(seat.drink_key, seat.drink_text),
                'gold_balance': new_balance,
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def order_food(user_id: int, seat_id: int, food_key: str) -> Tuple[Optional[dict], Optional[str]]:
        """给自己的在座冒险者点餐；旧食物先按已用时长结算，再由新食物替换。"""
        try:
            meta = AdventurerS1TavernSeatService._food_map().get(food_key)
            if not meta:
                return None, '菜单上没有这种食物'
            seat_ref = AdventurerS1TavernSeat.query.with_entities(
                AdventurerS1TavernSeat.adventurer_id,
            ).filter(AdventurerS1TavernSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '先安排冒险者入座才能点餐'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1TavernSeat.query.filter(
                AdventurerS1TavernSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not seat or seat.adventurer_id != seat_ref.adventurer_id:
                return None, '座位状态已经变化，请重试'
            if not adv or adv.owner_id != user_id:
                return None, '只能给自己的冒险者点餐'
            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                return None, '冒险者已经离开酒馆了'
            if adv.status == 'dead' or int(adv.hp or 0) <= 0:
                return None, '已阵亡的冒险者无法用餐'

            max_stamina = int(current_app.config.get('ADVENTURER_S1_MAX_STAMINA', 100))
            hp_full = int(adv.hp or 0) >= int(adv.max_hp or 1)
            stamina_full = int(adv.stamina or 0) >= max_stamina
            if (not int(meta.get('hp_restore', 0)) or hp_full) and (
                    not int(meta.get('stamina_restore', 0)) or stamina_full):
                return None, '这份食物能恢复的状态已经全满了'

            previous = AdventurerS1TavernSeatService._settle_food(seat, adv)
            AdventurerS1TavernSeatService._clear_food(seat)
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            new_balance, err = S1CurrencyService.deduct(
                adv.id, 'gold', float(meta['price']), source_type='tavern_food',
                related_id=seat_id, related_type='tavern_seat',
                description=f'酒馆点餐：{meta["name"]}', auto_commit=False,
            )
            if err:
                db_manager.rollback()
                if '余额不足' in err:
                    return None, f'金币不足，点不起这份「{meta["name"]}」'
                return None, err

            started_at = _portable_utc_now()
            seat.food_key = food_key
            # 三段文案按前端时间进度切换；持久化首段仅作旧客户端兼容。
            seat.food_text = meta['texts'][0] if meta.get('texts') else ''
            seat.food_started_at = started_at
            seat.food_expires_at = started_at + timedelta(minutes=int(meta['duration_minutes']))
            seat.food_settled_at = None
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            return {
                'seat_id': seat_id,
                'food': AdventurerS1TavernSeatService._food_brief(seat),
                'gold_balance': new_balance,
                'previous_food_settlement': previous,
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def settle_due_foods_job() -> int:
        """按冒险者加锁回收陈旧占座，并结算仍在座者的到期食物。"""
        now = _portable_utc_now()
        settled = 0
        cleared_adv_ids: set[int] = set()
        try:
            candidate_rows = AdventurerS1TavernSeat.query.with_entities(
                AdventurerS1TavernSeat.id, AdventurerS1TavernSeat.adventurer_id,
            ).filter(
                AdventurerS1TavernSeat.adventurer_id.isnot(None),
            ).order_by(AdventurerS1TavernSeat.id.asc()).all()
            if not candidate_rows:
                return 0

            seat_ids_by_adv: dict[int, list[int]] = {}
            for row in candidate_rows:
                seat_ids_by_adv.setdefault(int(row.adventurer_id), []).append(int(row.id))

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            from sqlalchemy.orm import load_only, noload

            # 固定 adv -> seat 锁序；被其它写路径占用的冒险者整组跳过，下轮重试。
            adv_ids = sorted(seat_ids_by_adv)
            existing_adv_ids = {
                int(row.id) for row in AdventurerS1.query.with_entities(
                    AdventurerS1.id,
                ).filter(AdventurerS1.id.in_(adv_ids)).all()
            }
            adv_rows = (
                AdventurerS1.query
                .options(
                    load_only(
                        AdventurerS1.id,
                        AdventurerS1.status,
                        AdventurerS1.building_instance_id,
                        AdventurerS1.stamina,
                        AdventurerS1.hp,
                        AdventurerS1.max_hp,
                    ),
                    noload('*'),
                )
                .filter(AdventurerS1.id.in_(adv_ids))
                .order_by(AdventurerS1.id.asc())
                .populate_existing()
                .with_for_update(skip_locked=True)
                .all()
            )
            adv_map = {adv.id: adv for adv in adv_rows}
            missing_adv_ids = set(adv_ids) - existing_adv_ids
            candidate_adv_by_seat = {
                seat_id: adv_id
                for adv_id, seat_ids in seat_ids_by_adv.items()
                if adv_id in adv_map or adv_id in missing_adv_ids
                for seat_id in seat_ids
            }
            candidate_ids = sorted(candidate_adv_by_seat)
            seats = (
                AdventurerS1TavernSeat.query
                .filter(
                    AdventurerS1TavernSeat.id.in_(candidate_ids),
                    AdventurerS1TavernSeat.adventurer_id.in_(
                        [*adv_map, *missing_adv_ids]
                    ),
                )
                .order_by(AdventurerS1TavernSeat.id.asc())
                .populate_existing()
                .with_for_update(skip_locked=True)
                .all()
            ) if candidate_ids else []
            active_adv_ids: set[int] = set()

            for seat in seats:
                expected_adv_id = candidate_adv_by_seat.get(seat.id)
                if not expected_adv_id or seat.adventurer_id != expected_adv_id:
                    continue
                adv = adv_map.get(expected_adv_id)
                if adv is None:
                    AdventurerS1TavernSeatService._clear_seat(seat)
                    continue

                due_food = bool(
                    seat.food_key
                    and seat.food_expires_at
                    and seat.food_expires_at <= now
                )
                should_clear = (
                    adv.status not in AdventurerS1Service.FREE_STATUSES
                    or bool(adv.building_instance_id)
                    or adv.id in active_adv_ids
                )

                if should_clear:
                    AdventurerS1TavernSeatService._clear_seat(seat)
                    cleared_adv_ids.add(adv.id)
                else:
                    active_adv_ids.add(adv.id)
                    if due_food:
                        AdventurerS1TavernSeatService._settle_food(seat, adv, now=now)
                        AdventurerS1TavernSeatService._clear_food(seat)

                if due_food:
                    settled += 1

            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '酒馆座位 tick 提交失败')
            if cleared_adv_ids:
                AdventurerS1TavernSeatService._emit_tavern_changed(
                    'tavern_auto_leave',
                    ids=sorted(cleared_adv_ids),
                )
            return settled
        except Exception:
            db_manager.rollback()
            raise

    @staticmethod
    def request_treat(user_id: int, target_seat_id: int, drink_key: str) -> Tuple[Optional[dict], Optional[str]]:
        """Ask another seated player's adventurer whether they accept a drink."""
        try:
            meta = S1_TAVERN_DRINK_MAP.get(drink_key)
            if not meta:
                return None, '酒单上没有这种酒'

            target_seat = AdventurerS1TavernSeat.query.session.get(AdventurerS1TavernSeat, target_seat_id)
            if not target_seat or not target_seat.adventurer_id:
                return None, '目标座位上没有冒险者'
            target_adv = AdventurerS1.query.session.get(AdventurerS1, target_seat.adventurer_id)
            if not target_adv or target_adv.owner_id is None:
                return None, '只能请其他玩家的冒险者喝东西'
            if target_adv.owner_id == user_id:
                return None, '不能向自己的冒险者发起请客请求'

            payer_seat = (
                AdventurerS1TavernSeat.query
                .join(AdventurerS1, AdventurerS1TavernSeat.adventurer_id == AdventurerS1.id)
                .filter(AdventurerS1.owner_id == user_id)
                .first()
            )
            if not payer_seat or not payer_seat.adventurer_id:
                return None, '你需要先让自己的冒险者入座'
            payer_adv = AdventurerS1.query.session.get(AdventurerS1, payer_seat.adventurer_id)
            if not payer_adv:
                return None, '请客的冒险者不存在'

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if payer_adv.status not in AdventurerS1Service.FREE_STATUSES or payer_adv.building_instance_id:
                return None, '请客的冒险者已经离开酒馆'
            if target_adv.status not in AdventurerS1Service.FREE_STATUSES or target_adv.building_instance_id:
                return None, '对方已经离开酒馆'

            existing_pending = AdventurerS1TavernTreatRequest.query.filter(
                AdventurerS1TavernTreatRequest.requester_user_id == user_id,
                AdventurerS1TavernTreatRequest.target_adventurer_id == target_adv.id,
                AdventurerS1TavernTreatRequest.status == 'pending',
            )
            existing_pending.update({
                AdventurerS1TavernTreatRequest.status: 'cancelled',
                AdventurerS1TavernTreatRequest.responded_at: _portable_utc_now(),
            }, synchronize_session=False)

            req = AdventurerS1TavernTreatRequest(
                requester_user_id=user_id,
                payer_adventurer_id=payer_adv.id,
                payer_seat_id=payer_seat.id,
                target_user_id=target_adv.owner_id,
                target_adventurer_id=target_adv.id,
                target_seat_id=target_seat.id,
                drink_key=drink_key,
            )
            ok, err = player_database_result(db_manager.add(req))
            if not ok:
                db_manager.rollback()
                return None, err
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err

            data = req.to_dict(
                payer_name=payer_adv.name,
                target_name=target_adv.name,
                drink=AdventurerS1TavernSeatService._drink_menu_item(drink_key),
            )
            AdventurerS1TavernSeatService._emit_tavern_changed(
                'tavern_treat_requested',
                request_id=req.id,
                target_user_id=target_adv.owner_id,
            )
            return data, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def respond_treat(user_id: int, request_id: int, accept: bool) -> Tuple[Optional[dict], Optional[str]]:
        """Accept or decline an incoming drink invitation."""
        try:
            req = AdventurerS1TavernTreatRequest.query.session.get(AdventurerS1TavernTreatRequest, request_id)
            if not req:
                return None, '请客请求不存在'
            if req.target_user_id != user_id:
                return None, '只能处理发给你的请客请求'
            if req.status != 'pending':
                return None, '请客请求已处理'

            if not accept:
                req.status = 'declined'
                req.responded_at = _portable_utc_now()
                ok, err = player_database_result(db_manager.commit())
                if not ok:
                    return None, err
                AdventurerS1TavernSeatService._emit_tavern_changed(
                    'tavern_treat_declined',
                    request_id=req.id,
                    requester_user_id=req.requester_user_id,
                )
                return req.to_dict(
                    drink=AdventurerS1TavernSeatService._drink_menu_item(req.drink_key),
                ), None

            meta = S1_TAVERN_DRINK_MAP.get(req.drink_key)
            if not meta:
                req.status = 'cancelled'
                req.responded_at = _portable_utc_now()
                db_manager.commit()
                return None, '酒单上没有这种酒'

            payer_adv = AdventurerS1.query.session.get(AdventurerS1, req.payer_adventurer_id)
            target_adv = AdventurerS1.query.session.get(AdventurerS1, req.target_adventurer_id)
            payer_seat = AdventurerS1TavernSeat.query.session.get(AdventurerS1TavernSeat, req.payer_seat_id)
            target_seat = AdventurerS1TavernSeat.query.session.get(AdventurerS1TavernSeat, req.target_seat_id)
            if not payer_adv or not target_adv or not payer_seat or not target_seat:
                return None, '请客请求已失效'
            if payer_adv.owner_id != req.requester_user_id or target_adv.owner_id != user_id:
                return None, '请客请求已失效'
            if payer_seat.adventurer_id != payer_adv.id or target_seat.adventurer_id != target_adv.id:
                return None, '有人已经离开原座位，请客请求已失效'

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if payer_adv.status not in AdventurerS1Service.FREE_STATUSES or payer_adv.building_instance_id:
                return None, '请客的人已经离开酒馆'
            if target_adv.status not in AdventurerS1Service.FREE_STATUSES or target_adv.building_instance_id:
                return None, '你已经离开酒馆'

            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            new_balance, err = S1CurrencyService.deduct(
                payer_adv.id, 'gold', float(meta['price']),
                source_type='tavern_treat',
                related_id=req.id,
                related_type='tavern_treat_request',
                description=f'酒馆请客：{meta["name"]} -> {target_adv.name}',
                auto_commit=False,
            )
            if err:
                db_manager.rollback()
                return None, err

            target_seat.drink_key = req.drink_key
            target_seat.drink_text = random.choice(meta['texts'])
            target_seat.ordered_at = _portable_utc_now()
            req.status = 'accepted'
            req.responded_at = _portable_utc_now()
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err

            drink = AdventurerS1TavernSeatService._drink_brief(
                target_seat.drink_key, target_seat.drink_text,
            )
            AdventurerS1TavernSeatService._emit_tavern_changed(
                'tavern_treat_accepted',
                request_id=req.id,
                requester_user_id=req.requester_user_id,
                target_user_id=user_id,
            )
            return {
                'request': req.to_dict(
                    payer_name=payer_adv.name,
                    target_name=target_adv.name,
                    drink=AdventurerS1TavernSeatService._drink_menu_item(req.drink_key),
                ),
                'seat_id': target_seat.id,
                'drink': drink,
                'payer_gold_balance': new_balance,
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
