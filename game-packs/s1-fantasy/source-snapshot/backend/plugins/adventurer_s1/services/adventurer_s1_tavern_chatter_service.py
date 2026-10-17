"""酒馆大堂环境闲聊：预生成文案池 + 按 tick 概率拼装。

入座冒险者会按时间桶（tick）有概率冒出一句闲聊，可引用同桌、隔壁桌或
大堂里其他在座冒险者的名字；若两人有好感度记录，会按亲疏档位挑选更贴切的
台词；若已点酒，还会按酒种 / 档次挑选台词。纯静态模板，零 AI token；
好感度分值不向玩家暴露。

占位符：
- {speaker}      当前座位上的冒险者
- {target}       对话对象（同桌 / 邻桌 / 其他在座者）
- {drink}        说话者眼前的酒名（未点单时为「空杯子」）
- {target_drink} 对话对象眼前的酒名（未点单时为「空杯子」）
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from flask import current_app

from plugins.adventurer_s1.models import AdventurerS1Relationship, AdventurerS1TavernSeat
from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import S1_TAVERN_DRINK_MAP

# scope: 从哪里找对话对象
#   solo / same_table / adjacent_bar / adjacent_table / any_seated
# relation: 好感度档位（any / close / friendly / neutral / strained / hostile / rival）
# speaker_drink / target_drink: any | none | has（是否已点酒）
# speaker_tier / target_tier: any | soft | mid | strong | luxury（按酒价分档，仅 has 时生效）
# speaker_drink_key / target_drink_key: 指定酒种 key（可选，如 dragon_flame）
S1_TAVERN_CHATTER_TEMPLATES: List[dict] = [
    # ── 独白 ──────────────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着杯底发呆，不知道在想什么。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}小声哼着走调的歌，周围人假装没听见。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把酒杯转了三圈，仍没想好下一句开场白。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}忽然打了个喷嚏，连忙解释「不是咒骂，是粉尘」。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}数着天花板裂缝，已经数到第十七道了。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把披风叠成枕头，决定再坐一会儿。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对着空椅子练习微笑，像在排练明天的谈判。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把最后一块面包掰成两半，一半收进怀里。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着门口，像是在等一个不会来的人。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用指尖蘸酒，在桌上画了个歪歪扭扭的地图。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}忽然长叹一口气，酒保装作没听见。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把剑靠在椅背上，提醒自己今晚不许拔剑。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}翻看旧伤疤，每道都像一本没写完的书。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}跟自己的影子碰了碰杯，说了句「敬活着」。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把帽檐压得很低，仿佛这样就能躲开明天的麻烦。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}数着口袋里还剩几枚铜板，表情逐渐严肃。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}忽然笑了一下，又迅速板起脸，像怕被人看见。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用叉子敲杯沿，敲出一段谁也听不懂的节奏。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把冒险日志摊开又合上，最终只写下两个字：待定。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对着烛火眯起眼，说「这光，像老家」。'},
    # ── 同桌 ──────────────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把花生推给{target}：「别客气，我请不起第二杯。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}压低声音问{target}：「你信昨天门口那个预言吗？」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}碰了碰杯，泡沫溅了一桌。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}指着窗外对{target}说：「看，月亮又圆了。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「明天还出发吗？」{target}看了眼两人的绷带：「先问问腿。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把{target}的酒杯扶正：「慢点喝，没人跟你抢。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}赌一枚铜板：谁先醉谁洗碗。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}坦白：「其实我今天差点没回来。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把地图转向{target}：「这条近。」{target}指着角落：「那是比例尺。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}学{target}的口头禅，学得惟妙惟肖，引来邻桌侧目。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}给{target}夹了块肉：「你今天话少，不像你。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}举杯：「敬咱俩还能坐在这儿喝酒。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}跟{target}回忆上次探险，笑得差点呛到。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}拍拍{target}肩膀：「明天你看前面，我负责看你有没有跑丢。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}低声对{target}说：「要是明天回不来，帮我把信交出去。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}把最后一口好酒让给{target}：「你比我更需要这杯。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}替{target}系紧护腕：「明天可以逞强，但得在我看得见的地方。」'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}礼貌性问{target}：「介意我坐这儿吗？」其实早就坐下了。'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}跟{target}点头致意，对话停在「今天天气不错」。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}把椅子往旁边挪了挪，尽量离{target}远一点。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}对{target}冷笑：「没想到你也来这种地方放松。」'},
    {'scope': 'same_table', 'relation': 'hostile', 'text': '{speaker}把酒杯重重放下：「{target}，别看我，看你自己的杯子。」'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}盯着{target}：「下次下本，各走各的，别拖后腿。」'},
    # ── 吧台邻座 ──────────────────────────────────────────────────────────
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}侧头对{target}说：「这家的麦芽啤，第三杯才开始像酒。」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}跟吧台边的{target}吐槽：「酒保今天脸色像刚输过牌。」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}问{target}：「你也是来躲雨的吗？」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}把花生袋递给{target}：「独酌无聊，分你一半。」'},
    {'scope': 'adjacent_bar', 'relation': 'friendly', 'text': '{speaker}冲{target}举了举杯：「吧台窄，友情宽。」'},
    {'scope': 'adjacent_bar', 'relation': 'close', 'text': '{speaker}低声跟{target}咬耳朵，酒保假装在擦杯子。'},
    {'scope': 'adjacent_bar', 'relation': 'hostile', 'text': '{speaker}挪开凳子，尽量不让肩膀碰到{target}。'},
    # ── 隔壁桌 ────────────────────────────────────────────────────────────
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}朝隔壁桌的{target}喊：「借个火——啊不对，借个故事。」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}对{target}说：「你们那桌笑得太大声了，分我点喜气。」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}问隔壁的{target}：「你那杯什么味的？闻着像蜂蜜。」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}跟{target}隔空碰杯，谁也没站起来。'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}提醒{target}：「你袖子沾到蜡烛了——现在没事了。」'},
    {'scope': 'adjacent_table', 'relation': 'friendly', 'text': '{speaker}冲{target}挥手：「下回拼个桌？我请客……可能。」'},
    {'scope': 'adjacent_table', 'relation': 'close', 'text': '{speaker}望着{target}那桌，轻声说：「看见你活着真好。」'},
    {'scope': 'adjacent_table', 'relation': 'strained', 'text': '{speaker}瞥了眼{target}，把话咽回去，只多喝了一口。'},
    {'scope': 'adjacent_table', 'relation': 'hostile', 'text': '{speaker}对{target}冷冷道：「别看我，我不是你的队友。」'},
    # ── 大堂任意其他在座者 ────────────────────────────────────────────────
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}远远冲{target}点头：「你也来透气啊。」'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}指着{target}对同伴嘀咕：「那位看着像刚下本。」'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}朝{target}举起杯子，算是打过招呼。'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}问{target}：「听说你昨天在公会挂单了？」'},
    {'scope': 'any_seated', 'relation': 'friendly', 'text': '{speaker}隔桌喊{target}：「过来坐啊，又不是不认识。」'},
    {'scope': 'any_seated', 'relation': 'friendly', 'text': '{speaker}冲{target}晃了晃空背包：「下次叫我，战利品装不下时我很可靠。」'},
    {'scope': 'any_seated', 'relation': 'close', 'text': '{speaker}看见{target}，下意识摸了摸自己的旧伤疤。'},
    {'scope': 'any_seated', 'relation': 'neutral', 'text': '{speaker}与{target}目光相接，又各自移开，像两条平行线。'},
    {'scope': 'any_seated', 'relation': 'strained', 'text': '{speaker}看见{target}，把帽檐又压低了一寸。'},
    {'scope': 'any_seated', 'relation': 'hostile', 'text': '{speaker}对{target}冷哼一声，酒保赶紧插话打圆场。'},
    {'scope': 'any_seated', 'relation': 'rival', 'text': '{speaker}盯着{target}的剑鞘：「换过新剑？还是同一把。」'},
    # ── 多句 / 对白感（仍是一行展示）────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}：「你信命运吗？」{target}：「信，但信它会给第二次机会。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}：「今天差点回不来。」{target}：「所以你在喝酒，我在听你活着。」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}隔桌问{target}：「你们还缺人吗？」{target}：「缺个认路的。」两桌同时沉默。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}自言自语：「再坐一刻钟，就回宿舍。」——一刻钟过去了，还在。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对吧边的空气说：「别劝了，我就喝一杯。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}跟酒保讨价还价：「今天能不能算我精神胜利？」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把骰子掷了又掷，直到掷出想要的点数才停手。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用面包蘸酒，说这是家乡吃法，没人反驳。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着火焰发呆，火苗里好像有没回来的队友。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}比赛谁先把杯沿上的泡沫舔干净。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}把毯子分给{target}一半：「夜里冷，别逞强。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}什么也没说，只是把{target}的酒杯续满了。'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}问{target}：「你那队准备好了？」答：「准备好出发，没准备好后果。」'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}朝{target}比了个「小心背后」的口型，然后自己先笑了。'},
    {'scope': 'any_seated', 'relation': 'friendly', 'text': '{speaker}隔老远冲{target}喊：「下次别抢我人头！」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}跟{target}吐槽今天的酒：「淡得像公会报告。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}在桌上用酒渍画了个笑脸，又迅速擦掉。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把硬币立起来，赌它能不能立到第二杯。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}跟{target}同桌吃饭，却全程只跟酒保说话。'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}对{target}举杯：「敬你活着——也敬我活得比你久。」'},
    # ── 新一批大堂闲话 ────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}认真擦了半天武器，最后发现拿的是餐刀。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把悬赏单折成纸船，又赶紧拆开铺平。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}向酒保要了一杯勇气，酒保端来一杯温水。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}练习了一遍胜利致辞，又默默准备了遗言。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}发现靴底粘着一枚金币，高兴了三秒——是巧克力纸。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}在账单背面写诗，写到酒钱那一行突然没了灵感。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你觉得酒馆里最安全的位置在哪？」{target}指了指门外。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把最后一颗花生推到桌子中央，和{target}展开了漫长对峙。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}坚称自己没醉，随后向{target}的帽子道了晚安。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}替{target}保管秘密，条件是再讲一遍细节。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}和{target}约好谁先发财谁请客，两人都觉得这顿省定了。'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}悄悄把自己的护身符塞给{target}，嘴上说只是借一天。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}和{target}同时伸手拿盐罐，又同时假装自己不需要。'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}问{target}：「比比谁先喝完？」{target}：「比谁先买单。」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}问{target}酒保是不是会读心，酒保立刻递来了账单。'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'text': '{speaker}与{target}同时叹气，发现两人的节拍出奇一致。'},
    {'scope': 'adjacent_bar', 'relation': 'friendly', 'text': '{speaker}把最离谱的传闻讲给{target}，并保证只添了三成。'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}向{target}借盐，最后连故事和下酒菜一起借走了。'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}听见{target}那桌笑，没听懂原因也跟着笑了。'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}宣布今晚不谈工作，大堂里立刻有人谈起了委托。'},
    {'scope': 'any_seated', 'relation': 'friendly', 'text': '{speaker}朝{target}比了个碰杯手势，两人隔着半个大堂各喝了一口。'},
    {'scope': 'any_seated', 'relation': 'hostile', 'text': '{speaker}和{target}隔空对视，酒保默默收走了附近的玻璃杯。'},
    # ── 准备出门探险 ──────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}检查行囊：绳索、火种、干粮，还有一份完全看不懂的地图。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把遗言塞进背包最深处，又在外面贴了张「不许用上」。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对着门口深吸一口气：「出去是冒险，留下是欠酒钱。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}数了三遍药水，第四遍决定相信前三遍的自己。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把指南针拍了两下，它终于坚定地指向了酒窖。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「路线定了吗？」{target}：「定了，迷路后往回走。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}和{target}约定明天谁也不许说「我有个大胆的想法」。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把补给清单递给{target}，上面第一项是「活着回来」。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}对{target}说：「明天跟紧我。」{target}：「上次说这话的人先迷路了。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}和{target}排练撤退暗号，最后决定还是直接喊「快跑」。'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}碰了碰{target}的杯沿：「明晚还在这里，这杯才算喝完。」'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}对{target}说：「明天各管各的。」却多准备了一份绷带。'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}对{target}扬眉：「明天看谁先找到宝箱。」{target}：「看谁先识破陷阱。」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'text': '{speaker}隔桌问{target}有没有多余火把，{target}反问有没有多余胆量。'},
    {'scope': 'adjacent_table', 'relation': 'friendly', 'text': '{speaker}朝{target}喊：「明早门口见，迟到的人负责背锅和锅。」'},
    {'scope': 'any_seated', 'relation': 'any', 'text': '{speaker}问{target}远征带什么最重要。{target}想了想：「带个会回头数人的队长。」'},
    {'scope': 'any_seated', 'relation': 'friendly', 'text': '{speaker}冲{target}举杯：「祝我们明天遇见宝藏，而不是宝藏的主人。」'},
    {'scope': 'any_seated', 'relation': 'close', 'text': '{speaker}远远提醒{target}：「明天别走最后一个，也别走第一个。」'},
    # ── 未点单 ──────────────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'none',
     'text': '{speaker}敲了敲桌面：「酒保，菜单呢？我还没想好喝什么。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'none',
     'text': '{speaker}干坐着发呆，手边连个杯子都没有。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'none',
     'text': '{speaker}翻了翻口袋，在心里默算今天还能点几杯。'},
    {'scope': 'same_table', 'relation': 'any', 'speaker_drink': 'none', 'target_drink': 'has',
     'text': '{speaker}羡慕地瞧着{target}手里的{target_drink}：「你先喝，我攒钱。」'},
    {'scope': 'same_table', 'relation': 'any', 'speaker_drink': 'has', 'target_drink': 'none',
     'text': '{speaker}晃了晃{drink}：「{target}，你不点一杯吗？」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'speaker_drink': 'none', 'target_drink': 'has',
     'text': '{speaker}隔桌问{target}：「你那杯{target_drink}香不香？」'},
    {'scope': 'any_seated', 'relation': 'any', 'speaker_drink': 'none',
     'text': '{speaker}扫过大堂里杯盏交错的桌子，还在犹豫要不要破费。'},
    # ── 按酒名 / 档次（speaker 已点酒）────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'has',
     'text': '{speaker}捧着{drink}，小口小口地喝，像在品尝一段慢时光。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'has',
     'text': '{speaker}盯着眼前的{drink}，忽然觉得今天也不算太糟。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink': 'has',
     'text': '{speaker}用指腹擦了擦杯壁，确认{drink}还是温的。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'soft',
     'text': '{speaker}举着{drink}自嘲：「今晚清醒，全靠这杯。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'soft',
     'text': '{speaker}跟邻桌解释：「我不是不能喝，是选了{drink}。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'mid',
     'text': '{speaker}一口{drink}下肚，肩膀明显松了下来。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'strong',
     'text': '{speaker}被{drink}辣得咳了一声，又忍不住抿了第二口。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'luxury',
     'text': '{speaker}端着{drink}，坐姿不知不觉端正了许多。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_tier': 'luxury',
     'text': '{speaker}对着{drink}里的倒影端详，像在审视一位贵客。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'dragon_flame',
     'text': '{speaker}被{drink}烫得眼眶发红，却竖起大拇指示意「值了」。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'abyss_tear',
     'text': '{speaker}盯着{drink}里幽暗的光，半天没舍得喝第二口。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'orange_juice',
     'text': '{speaker}吸溜一口{drink}，跟全酒馆宣布：「我今晚绝对清醒。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'honey_goat_milk',
     'text': '{speaker}捧着{drink}暖手，眼皮开始不争气地打架。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'old_friend_ale',
     'text': '{speaker}给对面的空杯斟了一点{drink}：「这杯算你的，老伙计。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'old_friend_ale',
     'text': '{speaker}举起{drink}望向旧队伍常坐的角落：「敬没回来的人，也敬还得出发的人。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'speaker_drink_key': 'old_friend_ale',
     'text': '{speaker}把{drink}轻轻碰向{target}的杯沿：「今晚不劝你忘，陪你记一会儿。」'},
    {'scope': 'same_table', 'relation': 'close', 'speaker_drink_key': 'old_friend_ale',
     'text': '{speaker}和{target}轮流说起老队友的糗事，笑到一半，又默契地安静下来。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'fallen_sakura_sake',
     'text': '{speaker}看着{drink}里的花瓣：「替你走完归途，也替你看看明年的春天。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'speaker_drink_key': 'fallen_sakura_sake',
     'text': '{speaker}分给{target}一片杯底的樱花：「记住他们，不等于留在原地。」'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'ember_vigil_wine',
     'text': '{speaker}用杯沿轻叩桌面三次：「换岗了，老伙计。剩下的夜交给我。」'},
    {'scope': 'same_table', 'relation': 'close', 'speaker_drink_key': 'ember_vigil_wine',
     'text': '{speaker}与{target}把{drink}放在两人之间，安静守完了一小段属于故人的夜。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'witch_brew',
     'text': '{speaker}看着{drink}变色，小声嘀咕：「这真的喝不死人吧？」'},
    {'scope': 'same_table', 'relation': 'any', 'speaker_drink': 'has', 'target_drink': 'has',
     'text': '{speaker}跟{target}碰杯：「你的{target_drink}，我的{drink}，干杯。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'speaker_drink': 'has', 'target_drink': 'has',
     'text': '{speaker}把自己的{drink}往{target}那边推了推：「尝尝，这杯适合你。」'},
    {'scope': 'same_table', 'relation': 'any', 'speaker_tier': 'luxury', 'target_drink': 'has',
     'text': '{speaker}晃着{drink}：「{target}，你那杯也不错，但我的更贵。」'},
    {'scope': 'same_table', 'relation': 'any', 'speaker_tier': 'soft', 'target_tier': 'strong',
     'text': '{speaker}举着{drink}看{target}闷{target_drink}：「你喝你的烈酒，我喝我的清醒。」'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'speaker_drink': 'has', 'target_drink': 'has',
     'text': '{speaker}侧头问{target}：「你这杯{target_drink}，比我的{drink}如何？」'},
    {'scope': 'adjacent_table', 'relation': 'any', 'speaker_drink': 'has',
     'text': '{speaker}举起{drink}朝隔壁示意，算是对{target}打过招呼。'},
    {'scope': 'any_seated', 'relation': 'any', 'speaker_drink': 'has', 'target_drink': 'none',
     'text': '{speaker}晃了晃{drink}，冲{target}挑眉：「还不点？我等你。」'},
    {'scope': 'any_seated', 'relation': 'friendly', 'speaker_drink_key': 'bard_champagne',
     'text': '{speaker}举着{drink}隔桌喊{target}：「今天值得开一瓶！」'},
    {'scope': 'same_table', 'relation': 'close', 'speaker_drink': 'has', 'target_drink': 'has',
     'text': '{speaker}把{drink}和{target}的{target_drink}轻轻碰在一起，什么都没说。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'frost_icewine',
     'text': '{speaker}贴着{drink}冰凉的杯壁，像握住了一小块冬天。'},
    {'scope': 'solo', 'relation': 'any', 'speaker_drink_key': 'mercenary_vodka',
     'text': '{speaker}一口{drink}闷下去，嗓门立刻大了一倍。'},
    {'scope': 'adjacent_bar', 'relation': 'any', 'speaker_drink_key': 'barley_ale',
     'text': '{speaker}拍拍{drink}的杯壁：「第三杯才开始像酒，懂行的都懂。」'},
]


def _relation_bucket(score: Optional[int]) -> str:
    """把好感度映射为模板 relation 档位（不向玩家暴露具体分值）。"""
    if score is None:
        return 'neutral'
    if score >= 80:
        return 'close'
    if score >= 40:
        return 'close'
    if score >= 10:
        return 'friendly'
    if score > -10:
        return 'neutral'
    if score > -40:
        return 'strained'
    if score > -80:
        return 'hostile'
    return 'rival'


def _relation_matches(template_relation: str, bucket: str) -> bool:
    if template_relation == 'any':
        return True
    if template_relation == bucket:
        return True
    # close 模板也可用于更高好感；friendly 可接受 close
    if template_relation == 'friendly' and bucket == 'close':
        return True
    if template_relation == 'hostile' and bucket == 'rival':
        return True
    if template_relation == 'strained' and bucket in ('hostile', 'rival'):
        return True
    return False


def _pair_key(a_id: int, b_id: int) -> Tuple[int, int]:
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def _drink_tier(drink_key: Optional[str]) -> Optional[str]:
    """按酒价分档：soft ≤15 / mid ≤80 / strong ≤200 / luxury 201+。"""
    if not drink_key:
        return None
    meta = S1_TAVERN_DRINK_MAP.get(drink_key)
    if not meta:
        return None
    price = int(meta['price'])
    if price <= 15:
        return 'soft'
    if price <= 80:
        return 'mid'
    if price <= 200:
        return 'strong'
    return 'luxury'


def _drink_name(drink_key: Optional[str]) -> str:
    if not drink_key:
        return ''
    meta = S1_TAVERN_DRINK_MAP.get(drink_key)
    return meta['name'] if meta else ''


@dataclass
class _SeatCtx:
    seat: AdventurerS1TavernSeat
    adv_id: int
    adv_name: str
    drink_key: Optional[str] = None
    drink_name: str = ''


def _drink_ctx_matches(tpl: dict, ctx: _SeatCtx, role: str) -> bool:
    """校验模板对 speaker / target 的酒饮条件。"""
    req = tpl.get(f'{role}_drink', 'any')
    if req == 'none' and ctx.drink_key:
        return False
    if req == 'has' and not ctx.drink_key:
        return False

    specific = tpl.get(f'{role}_drink_key')
    if specific and ctx.drink_key != specific:
        return False

    tier_req = tpl.get(f'{role}_tier', 'any')
    if tier_req != 'any':
        if not ctx.drink_key:
            return False
        if _drink_tier(ctx.drink_key) != tier_req:
            return False
    return True


class AdventurerS1TavernChatterService:
    """大堂环境闲聊生成器。"""

    @staticmethod
    def _tick_bucket(now: Optional[datetime] = None) -> int:
        interval = int(current_app.config.get('ADVENTURER_S1_TAVERN_CHATTER_TICK_SECONDS', 300))
        interval = max(10, interval)
        ts = (now or _portable_utc_now()).timestamp()
        return int(ts) // interval

    @staticmethod
    def _load_relationship_map(adv_ids: Sequence[int]) -> Dict[Tuple[int, int], int]:
        ids = list({int(i) for i in adv_ids})
        if len(ids) < 2:
            return {}
        rows = (
            AdventurerS1Relationship.query
            .filter(
                AdventurerS1Relationship.adventurer_a_id.in_(ids),
                AdventurerS1Relationship.adventurer_b_id.in_(ids),
            )
            .with_entities(
                AdventurerS1Relationship.adventurer_a_id,
                AdventurerS1Relationship.adventurer_b_id,
                AdventurerS1Relationship.score,
            )
            .all()
        )
        return {(a, b): score for a, b, score in rows}

    @staticmethod
    def _build_context(
        seats: Sequence[AdventurerS1TavernSeat],
        adv_name_map: Dict[int, str],
        stale_seat_ids: Iterable[int],
    ) -> List[_SeatCtx]:
        stale = set(stale_seat_ids)
        ctx: List[_SeatCtx] = []
        for seat in seats:
            if not seat.adventurer_id or seat.id in stale:
                continue
            name = adv_name_map.get(seat.adventurer_id)
            if not name:
                continue
            ctx.append(_SeatCtx(
                seat=seat,
                adv_id=seat.adventurer_id,
                adv_name=name,
                drink_key=seat.drink_key,
                drink_name=_drink_name(seat.drink_key),
            ))
        return ctx

    @staticmethod
    def _candidates_for_scope(
        speaker: _SeatCtx,
        scope: str,
        occupied: List[_SeatCtx],
    ) -> List[_SeatCtx]:
        if scope == 'solo':
            return []
        others = [c for c in occupied if c.adv_id != speaker.adv_id]
        if not others:
            return []

        if scope == 'same_table':
            if not speaker.seat.table_key:
                return []
            return [c for c in others if c.seat.table_key == speaker.seat.table_key]

        if scope == 'adjacent_bar':
            if speaker.seat.zone != 'bar':
                return []
            order = speaker.seat.sort_order
            return [
                c for c in others
                if c.seat.zone == 'bar' and abs(c.seat.sort_order - order) == 1
            ]

        if scope == 'adjacent_table':
            if speaker.seat.zone == 'bar':
                return [c for c in others if c.seat.zone == 'table']
            return [c for c in others if c.seat.table_key != speaker.seat.table_key]

        if scope == 'any_seated':
            return others

        # 兼容模板里误写的 bar → 视为 solo
        return []

    @staticmethod
    def _format_line(tpl: dict, speaker: _SeatCtx, target: Optional[_SeatCtx]) -> str:
        drink_label = speaker.drink_name or '空杯子'
        target_drink_label = (target.drink_name if target else '') or '空杯子'
        return tpl['text'].format(
            speaker=speaker.adv_name,
            target=target.adv_name if target else '',
            drink=drink_label,
            target_drink=target_drink_label,
        )

    @staticmethod
    def _pick_template_and_target(
        speaker: _SeatCtx,
        occupied: List[_SeatCtx],
        rel_map: Dict[Tuple[int, int], int],
        rng: random.Random,
    ) -> Optional[str]:
        viable: List[Tuple[dict, Optional[_SeatCtx]]] = []
        for tpl in S1_TAVERN_CHATTER_TEMPLATES:
            if not _drink_ctx_matches(tpl, speaker, 'speaker'):
                continue

            scope = tpl.get('scope', 'solo')
            if scope == 'solo':
                if _relation_matches(tpl.get('relation', 'any'), 'neutral'):
                    viable.append((tpl, None))
                continue

            candidates = AdventurerS1TavernChatterService._candidates_for_scope(
                speaker, scope, occupied,
            )
            if not candidates:
                continue

            rng.shuffle(candidates)
            for cand in candidates:
                if not _drink_ctx_matches(tpl, cand, 'target'):
                    continue
                score = rel_map.get(_pair_key(speaker.adv_id, cand.adv_id))
                bucket = _relation_bucket(score)
                if _relation_matches(tpl.get('relation', 'any'), bucket):
                    viable.append((tpl, cand))
                    break

        if not viable:
            return None

        tpl, target = rng.choice(viable)
        return AdventurerS1TavernChatterService._format_line(tpl, speaker, target)

    @staticmethod
    def generate_for_seats(
        seats: Sequence[AdventurerS1TavernSeat],
        adv_name_map: Dict[int, str],
        stale_seat_ids: Iterable[int],
        now: Optional[datetime] = None,
    ) -> Dict[int, Optional[str]]:
        """为每个有效占座座位生成闲聊文案。返回 seat_id -> chatter（无则 None）。"""
        prob = float(current_app.config.get('ADVENTURER_S1_TAVERN_CHATTER_PROB', 0.35))
        prob = min(max(prob, 0.0), 1.0)
        bucket = AdventurerS1TavernChatterService._tick_bucket(now)

        occupied = AdventurerS1TavernChatterService._build_context(
            seats, adv_name_map, stale_seat_ids,
        )
        if not occupied:
            return {}

        rel_map = AdventurerS1TavernChatterService._load_relationship_map(
            [c.adv_id for c in occupied],
        )

        result: Dict[int, Optional[str]] = {}
        for ctx in occupied:
            rng = random.Random(f's1-chatter:{ctx.seat.id}:{ctx.adv_id}:{bucket}')
            if rng.random() > prob:
                result[ctx.seat.id] = None
                continue
            line = AdventurerS1TavernChatterService._pick_template_and_target(
                ctx, occupied, rel_map, rng,
            )
            result[ctx.seat.id] = line
        return result
