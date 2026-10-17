"""Player-facing ceremony contracts for the S1 Star Lantern Hall."""

from datetime import date, datetime, time, timedelta, timezone
import logging
from uuid import uuid4

from flask import current_app

from db_repository import DatabaseError, db_manager
from db_repository.s1_ceremony_hall_repository import (
    count_visits,
    count_visits_for_user,
    create_attendance,
    create_visit,
    get_attendance,
    get_attendance_by_visit_token,
    get_banquet_companion_context,
    get_ceremony_gift_log,
    get_latest_visit_for_user,
    get_owned_adventurer_name,
    get_player_state,
    get_profile,
    get_visit_by_request_token,
    list_open_birthday_tables,
    list_visits,
    lock_owned_adventurer,
    lock_user,
    mark_attendance_gift_delivered,
    save_birthday_profile,
    set_attendance_visitors_open,
)
from plugins.adventurer_s1.seed import (
    BIRTHDAY_CAKE_ITEM_DESCRIPTION,
    BIRTHDAY_CAKE_ITEM_NAME,
)
from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
from utils.time_utils import BEIJING_TZ, to_utc_iso


logger = logging.getLogger(__name__)

BIRTHDAY_CEREMONY_TYPE = 'birthday_banquet'
BIRTHDAY_GIFT_SOURCE_TYPE = 'ceremony_birthday'
BIRTHDAY_GIFT_RELATED_TYPE = 'ceremony_attendance'

_WISH_OPTIONS = {
    'homefire': {
        'label': '归火之愿',
        'verse': '愿每一次归来，都有炉火与一张留给你的椅子。',
        'sigil': '♨',
    },
    'companions': {
        'label': '同行之愿',
        'verse': '愿漫长道路上，总有人与你并肩看见晨光。',
        'sigil': '✦',
    },
    'courage': {
        'label': '勇气之愿',
        'verse': '愿下一扇未知的门前，你仍有推门而入的勇气。',
        'sigil': '⚔',
    },
    'wonder': {
        'label': '远星之愿',
        'verse': '愿走过熟悉的地图后，远方仍有未命名的星。',
        'sigil': '✧',
    },
    'rest': {
        'label': '安然之愿',
        'verse': '愿需要停下的时候，你总能找到不必逞强的一夜。',
        'sigil': '☽',
    },
    'laughter': {
        'label': '笑声之愿',
        'verse': '愿严肃的长路之间，总有一些值得笑很久的小事。',
        'sigil': '♫',
    },
    'new_chapter': {
        'label': '新章之愿',
        'verse': '愿翻过这一页后，新的故事由你自己写下开头。',
        'sigil': '⌁',
    },
    'gentleness': {
        'label': '自在之愿',
        'verse': '愿你对世界保有锋芒，也记得把一点温柔留给自己。',
        'sigil': '◇',
    },
}

_DEFAULT_FEAST_KEY = 'hearth_homecoming'

_FEAST_THEMES = {
    'hearth_homecoming': {
        'label': '炉火归乡席',
        'subtitle': '热汤、烤盘与一盏等你回来的灯',
        'description': '最像深夜归队的一桌：从刚出炉的面包吃到寿烛甜塔，适合与同行者慢慢分食。',
        'sigil': '♨',
        'dishes': (
            {
                'key': 'hearth-bread',
                'course_label': '开席暖盘',
                'name': '蜂蜜炉火面包',
                'description': '刚出炉的圆面包，撕开时带着橙花蜜与融化黄油的香气。',
                'sigil': '◌',
            },
            {
                'key': 'night-stew',
                'course_label': '长桌主菜',
                'name': '长夜慢炖锅',
                'description': '牛肉、根茎与香草在陶锅里守了一整天，热气足够暖完整张桌。',
                'sigil': '♨',
            },
            {
                'key': 'sharing-roast',
                'course_label': '同行分享',
                'name': '迷迭香分享烤盘',
                'description': '烤禽与焦甜时蔬盛在同一只大盘里，适合从桌中央分给每个人。',
                'sigil': '◇',
            },
            {
                'key': 'star-cake',
                'course_label': '寿烛甜点',
                'name': '星糖奶油生日塔',
                'description': '塔顶只留一簇小小星火，等寿星在祝愿之后亲手吹熄。',
                'sigil': '✦',
            },
        ),
    },
    'moonlit_garden': {
        'label': '月桂庭园席',
        'subtitle': '清香、月桂与窗边吹进来的晚风',
        'description': '为想把生日过得轻盈一点的寿星准备；香草、烤鱼、果篮与白巧甜点依次上桌。',
        'sigil': '❦',
        'dishes': (
            {
                'key': 'laurel-flatbread',
                'course_label': '开席小点',
                'name': '月桂香草薄饼',
                'description': '薄饼烤得微脆，撒上月桂、软奶酪与刚剪下来的嫩香草。',
                'sigil': '❦',
            },
            {
                'key': 'lemon-river-fish',
                'course_label': '庭园主菜',
                'name': '月露柠檬烤鱼',
                'description': '河鱼裹着柠檬、海盐与莳萝慢烤，银亮外皮下仍留着清甜汁水。',
                'sigil': '☽',
            },
            {
                'key': 'garden-cheese-board',
                'course_label': '桌边分享',
                'name': '花园奶酪果篮',
                'description': '软硬奶酪、葡萄、无花果与坚果铺满木板，想吃什么便自己取。',
                'sigil': '◇',
            },
            {
                'key': 'moon-petal-cake',
                'course_label': '寿烛甜点',
                'name': '月瓣白巧生日糕',
                'description': '白巧与香草奶油叠成月瓣，切开后能看见一层淡金色柑橘酱。',
                'sigil': '✧',
            },
        ),
    },
    'starberry_tea': {
        'label': '星莓甜梦席',
        'subtitle': '热可可、莓果与一场不赶时间的茶会',
        'description': '甜香里也留着几道暖胃小食，适合围着茶壶聊天，把生日过成一场柔软的午后梦。',
        'sigil': '✧',
        'dishes': (
            {
                'key': 'cloud-corn-soup',
                'course_label': '暖胃小盅',
                'name': '云朵玉米浓汤',
                'description': '甜玉米浓汤上浮着一勺细软奶沫，胡椒只撒了很轻的一圈。',
                'sigil': '◌',
            },
            {
                'key': 'starberry-scones',
                'course_label': '茶席烘焙',
                'name': '星莓奶油松饼',
                'description': '温热松饼夹着酸甜星莓酱，奶油装在小银盏里由大家随意添。',
                'sigil': '✦',
            },
            {
                'key': 'honey-fruit-cups',
                'course_label': '同行分享',
                'name': '蜂蜜星果杯',
                'description': '切好的莓果、梨与蜜渍橙皮盛进玻璃杯，颜色像落进桌边的星群。',
                'sigil': '◇',
            },
            {
                'key': 'cocoa-constellation-cake',
                'course_label': '寿烛甜点',
                'name': '星图可可生日糕',
                'description': '深可可蛋糕上用糖霜连出一幅星图，寿星可以挑一颗星先切。',
                'sigil': '✧',
            },
        ),
    },
    'roadside_harvest': {
        'label': '远行丰收席',
        'subtitle': '旅粮、铁锅与刚从篝火边带回来的香气',
        'description': '份量扎实、适合热闹分食的一桌，像队伍平安归来后把沿途收获全摆上了长桌。',
        'sigil': '⌖',
        'dishes': (
            {
                'key': 'traveler-pie',
                'course_label': '开席咸点',
                'name': '香料旅人手派',
                'description': '酥皮里包着肉末、洋葱与香料，单手拿着也不会耽误继续讲故事。',
                'sigil': '⌖',
            },
            {
                'key': 'mushroom-ironpot-rice',
                'course_label': '长桌主食',
                'name': '铁锅蘑菇炖饭',
                'description': '森林蘑菇与谷米在铁锅里煨到绵软，锅底留着一圈焦香。',
                'sigil': '♨',
            },
            {
                'key': 'ember-roots',
                'course_label': '篝火烤盘',
                'name': '火炙玉米与蜜烤根菜',
                'description': '玉米、南瓜与根菜刷上香草蜜，在火边烤出深浅不一的焦边。',
                'sigil': '◇',
            },
            {
                'key': 'campfire-apple-pie',
                'course_label': '寿烛甜点',
                'name': '篝火苹果生日派',
                'description': '苹果与肉桂藏在厚酥皮下，揭开派顶时会冒出像营火一样的热气。',
                'sigil': '✦',
            },
        ),
    },
    'midnight_tea': {
        'label': '午夜无酒茶席',
        'subtitle': '暖茶、咸点与可以一直续杯的安静长夜',
        'description': '没有酒也不减仪式感；侍者会把茶壶留在桌边，适合慢慢吃、慢慢说话。',
        'sigil': '◒',
        'dishes': (
            {
                'key': 'moon-salt-biscuits',
                'course_label': '开席茶点',
                'name': '黄油月盐饼',
                'description': '黄油饼干撒着细盐与芝麻，咸香得正好，适合配第一壶热茶。',
                'sigil': '☽',
            },
            {
                'key': 'pumpkin-cream-cup',
                'course_label': '暖胃小盅',
                'name': '奶油南瓜小盅',
                'description': '烤南瓜打成细滑浓汤，杯口搁着一根迷迭香脆棒。',
                'sigil': '♨',
            },
            {
                'key': 'three-cheese-board',
                'course_label': '桌边分享',
                'name': '三色奶酪拼盘',
                'description': '三种熟成程度不同的奶酪配坚果与黑麦脆片，越聊越能吃出变化。',
                'sigil': '◇',
            },
            {
                'key': 'laurel-honey-jelly',
                'course_label': '寿烛甜点',
                'name': '月桂蜂蜜茶冻',
                'description': '透亮茶冻里封着一瓣可食用花，淋蜜后会在烛光下轻轻发亮。',
                'sigil': '✧',
            },
        ),
    },
}

_TAVERN_BLESSINGS = {
    'homefire': (
        '今夜，深渊酒馆以炉火、长桌与满杯作证：愿{name}往后的归途都有灯亮，'
        '推门时总有人认得你的脚步，落座时总有一碗热汤等你。'
    ),
    'companions': (
        '今夜，深渊酒馆以并肩而坐的人们作证：愿{name}今后的每段路都不必独行，'
        '身侧有可信的脚步，桌边永远有人为你留座。'
    ),
    'courage': (
        '今夜，深渊酒馆以未熄的寿烛作证：愿{name}在不得不向前时仍能听见自己的心，'
        '在需要回头时也不以此为怯；愿每次穿过长夜，都还带着名字与笑声回来。'
    ),
    'wonder': (
        '今夜，深渊酒馆以窗外的远星作证：愿{name}仍对地图之外的故事与下一次相遇保有好奇；'
        '愿世界总有一盏未见过的灯，等你亲手点亮。'
    ),
    'rest': (
        '今夜，深渊酒馆以安静守在门边的灯火作证：愿{name}在疲倦时不必解释，也不必逞强；'
        '愿每一次停步都不是掉队，而是有人替你看守行囊的一夜。'
    ),
    'laughter': (
        '今夜，深渊酒馆以杯盘间没忍住的笑声作证：愿{name}在漫长而认真的道路上，'
        '总能遇见一些不必有用、只值得笑很久的小事。'
    ),
    'new_chapter': (
        '今夜，深渊酒馆以刚刚翻开的空白礼簿作证：愿{name}不被旧页束缚，'
        '也不必急着写满以后；愿新一章的第一句话，始终由你亲手落笔。'
    ),
    'gentleness': (
        '今夜，深渊酒馆以寿烛旁柔软的微光作证：愿{name}仍有保护珍视之物的锋芒，'
        '也愿你在无人催促的时刻，把同样的耐心与温柔留给自己。'
    ),
}

_VISIT_ACTION_GROUPS = {
    'company': {
        'label': '在炉火边坐坐',
        'description': '拉开椅子、暖暖手，让寿星身边多一点陪伴。',
        'sigil': '◇',
    },
    'feast': {
        'label': '从长桌分点吃的',
        'description': '面包、炖锅、甜塔与热饮，都可以慢慢分着尝。',
        'sigil': '♨',
    },
    'story': {
        'label': '聊聊路上的事',
        'description': '讲见闻、摊地图，也可以逗桌边的人笑一笑。',
        'sigil': '✧',
    },
    'music': {
        'label': '给长桌添点乐声',
        'description': '哼旧调、敲杯沿，让今晚有一小段属于这桌的旋律。',
        'sigil': '♫',
    },
    'game': {
        'label': '玩个不计输赢的小游戏',
        'description': '只图桌边热闹，不记胜负，也没有任何奖励或消耗。',
        'sigil': '♢',
    },
    'blessing': {
        'label': '把祝愿留在桌边',
        'description': '举杯、折星，把想说的话交给今夜的灯火。',
        'sigil': '✦',
    },
}

_VISIT_ACTIONS = {
    'pull_up_chair': {
        'group_key': 'company',
        'label': '拉开椅子坐一会儿',
        'description': '不赶时间，只在炉火边陪寿星安静坐一会儿。',
        'sigil': '◇',
        'narrative': '{name}拉开一把空椅子，在桌边坐了一会儿。',
    },
    'warm_by_fire': {
        'group_key': 'company',
        'label': '挪到炉火边暖暖手',
        'description': '把旅途的寒气留在门外，围着炉火歇一阵。',
        'sigil': '♨',
        'narrative': '{name}把椅子朝炉火挪近一些，伸手烘了烘一路带来的寒气。',
    },
    'light_candle': {
        'group_key': 'company',
        'label': '替长桌添一盏小灯',
        'description': '不必多说，把一枚温暖的光点留给这张生日桌。',
        'sigil': '◌',
        'narrative': '{name}替长桌添了一盏小灯，让今夜又亮了一点。',
    },
    'watch_lanterns': {
        'group_key': 'company',
        'label': '陪寿星看看窗外星灯',
        'description': '把话停一会儿，一起看看礼堂窗外沿街亮起的灯。',
        'sigil': '☽',
        'narrative': '{name}陪寿星在窗边看了一会儿星灯，让桌边安静得很舒服。',
    },
    'share_dish': {
        'group_key': 'feast',
        'label': '把热食推到桌中央',
        'description': '从酒馆长桌分一份热气腾腾的食物给大家。',
        'sigil': '♨',
        'narrative': '{name}把一盘热食推到桌中央，招呼大家趁热分着吃。',
    },
    'break_honey_bread': {
        'group_key': 'feast',
        'label': '掰一块蜂蜜炉火面包',
        'description': '趁面包还热，把酥脆的边角分给桌边的人。',
        'sigil': '◌',
        'narrative': '{name}掰开一块还冒着热气的蜂蜜炉火面包，沿桌边分了出去。',
    },
    'ladle_night_stew': {
        'group_key': 'feast',
        'label': '盛一碗长夜慢炖锅',
        'description': '给寿星添一碗浓汤，也替晚来的旅人留一勺。',
        'sigil': '♨',
        'narrative': '{name}从长夜慢炖锅里盛出一碗浓汤，先放到了寿星手边。',
    },
    'slice_star_cake': {
        'group_key': 'feast',
        'label': '分一角星糖生日塔',
        'description': '切开缀着星糖的甜塔，让每个人都尝到一点。',
        'sigil': '✦',
        'narrative': '{name}替大家分开星糖生日塔，把缀着奶油与糖星的一角留给寿星。',
    },
    'pour_spiced_cider': {
        'group_key': 'feast',
        'label': '倒一杯香料苹果饮',
        'description': '给不想饮酒的人也添一只暖杯，照样可以碰杯。',
        'sigil': '◒',
        'narrative': '{name}倒了几杯温热的香料苹果饮，推来一杯与寿星轻轻相碰。',
    },
    'butter_roast_potato': {
        'group_key': 'feast',
        'label': '夹一颗黄油烤小薯',
        'description': '把烤得金黄的小薯滚过香草黄油，趁热分到寿星盘里。',
        'sigil': '◇',
        'narrative': '{name}夹起一颗滚过香草黄油的烤小薯，趁热放进寿星盘里。',
    },
    'split_berry_tart': {
        'group_key': 'feast',
        'label': '分一块星莓小挞',
        'description': '把酸甜莓果挞切成小角，桌边每个人都能尝到一口。',
        'sigil': '✧',
        'narrative': '{name}把一枚星莓小挞细细分开，给桌边每个人都留了一小角。',
    },
    'steep_moon_tea': {
        'group_key': 'feast',
        'label': '替大家续一壶月桂热茶',
        'description': '茶壶可以一直留在桌边，想喝的时候便再添一杯。',
        'sigil': '◒',
        'narrative': '{name}重新沏了一壶月桂热茶，替空下来的杯子逐一续满。',
    },
    'share_story': {
        'group_key': 'story',
        'label': '讲一段旅途见闻',
        'description': '说说最近路上遇见的怪事、风景或值得一笑的人。',
        'sigil': '✧',
        'narrative': '{name}靠近炉火，讲起一段最近旅途上的见闻。',
    },
    'unfold_map': {
        'group_key': 'story',
        'label': '摊开地图圈个远方',
        'description': '在旧地图上指一处没去过的地方，聊聊下一段路。',
        'sigil': '⌖',
        'narrative': '{name}在杯盘之间摊开一张旧地图，圈出一处想与大家再去看看的远方。',
    },
    'tell_tavern_joke': {
        'group_key': 'story',
        'label': '讲个酒馆里的冷笑话',
        'description': '不保证好笑，但今晚有人愿意捧场就够了。',
        'sigil': '☽',
        'narrative': '{name}讲了一个不太高明的酒馆笑话，桌边还是有人很给面子地笑出了声。',
    },
    'sketch_table_portrait': {
        'group_key': 'story',
        'label': '画一张歪歪扭扭的同桌速写',
        'description': '不求画得像，只把今夜围坐的样子留在纸角。',
        'sigil': '⌁',
        'narrative': '{name}借来纸笔，画下一张歪歪扭扭却很热闹的同桌速写。',
    },
    'trade_small_riddle': {
        'group_key': 'story',
        'label': '出一道没有难度的小谜语',
        'description': '答案可以很离谱，只要能让整桌人接着往下猜。',
        'sigil': '?',
        'narrative': '{name}出了一道几乎没有难度的小谜语，大家却故意猜了许多离谱答案。',
    },
    'hum_tavern_tune': {
        'group_key': 'music',
        'label': '哼一段酒馆旧调',
        'description': '声音不用很响，够让同桌的人听见就好。',
        'sigil': '♫',
        'narrative': '{name}轻轻哼起一段不知从哪位旅人那里学来的酒馆旧调。',
    },
    'tap_mug_rhythm': {
        'group_key': 'music',
        'label': '用杯沿敲一段节拍',
        'description': '木杯、银勺和指节都能加入，敲乱了也照样继续。',
        'sigil': '♪',
        'narrative': '{name}用杯沿敲起一段轻快节拍，没多久整张桌都跟着敲了起来。',
    },
    'request_lute_interlude': {
        'group_key': 'music',
        'label': '请驻唱弹一小段鲁特琴',
        'description': '不用点完整曲目，只让琴声在祝愿之间经过一会儿。',
        'sigil': '♫',
        'narrative': '{name}朝驻唱招了招手，请来一小段温柔的鲁特琴间奏。',
    },
    'choose_tavern_refrain': {
        'group_key': 'music',
        'label': '挑一句大家都会的副歌',
        'description': '会不会唱都没关系，唱到熟悉的那句再一起加入。',
        'sigil': '♬',
        'narrative': '{name}挑了一句大家多少都会的副歌，桌边很快响起参差却热闹的合唱。',
    },
    'play_story_chain': {
        'group_key': 'game',
        'label': '接一轮一句话冒险故事',
        'description': '每人只添一句，看这趟临时冒险最后会走到哪里。',
        'sigil': '✧',
        'narrative': '{name}起了个一句话冒险故事，整桌人你一句我一句地把它讲去了远方。',
    },
    'guess_hidden_token': {
        'group_key': 'game',
        'label': '猜猜木片藏在哪只杯下',
        'description': '猜中也只有一阵笑声，不计胜负，更不会发放奖励。',
        'sigil': '◌',
        'narrative': '{name}把一枚木片藏进杯影里，请寿星猜它最后停在了哪只杯下。',
    },
    'roll_wooden_dice': {
        'group_key': 'game',
        'label': '掷一把不计分的木骰',
        'description': '骰点只拿来编故事，不换奖品，也不改变任何数值。',
        'sigil': '♢',
        'narrative': '{name}掷出几枚木骰，大家照着点数随口编了一段不计输赢的小插曲。',
    },
    'build_card_tower': {
        'group_key': 'game',
        'label': '一起搭一座纸牌小塔',
        'description': '倒了就笑着重来，最后能搭几层并不重要。',
        'sigil': '⌂',
        'narrative': '{name}在杯盘空隙里搭起一座纸牌小塔，倒下后又和大家笑着重搭。',
    },
    'raise_glass': {
        'group_key': 'blessing',
        'label': '举杯说声生日快乐',
        'description': '让杯沿轻轻一碰，把一句简单的祝福留在今夜。',
        'sigil': '✦',
        'narrative': '{name}端起杯子，向寿星认真说了一声生日快乐。',
    },
    'fold_paper_star': {
        'group_key': 'blessing',
        'label': '折一枚纸星压在杯边',
        'description': '把没说出口的好愿望，折进一枚小小纸星。',
        'sigil': '✧',
        'narrative': '{name}折了一枚小小纸星，仔细压在寿星的杯边。',
    },
    'tie_wish_ribbon': {
        'group_key': 'blessing',
        'label': '把祝愿系上星灯绳',
        'description': '挑一段细缎带，让它替这句话在灯下多留一会儿。',
        'sigil': '⌁',
        'narrative': '{name}把一段写着祝愿的细缎带系上星灯绳，任它在暖风里轻轻摇晃。',
    },
    'sign_guestbook': {
        'group_key': 'blessing',
        'label': '在长桌礼簿写下名字',
        'description': '留下一笔来过的痕迹，也可以顺手画一枚自己的小记号。',
        'sigil': '✎',
        'narrative': '{name}在长桌礼簿上认真写下名字，又在旁边添了一枚小小记号。',
    },
    'seal_next_year_letter': {
        'group_key': 'blessing',
        'label': '封一封给明年此刻的短信',
        'description': '只写一句也可以，让酒馆替寿星把期待收进纸封。',
        'sigil': '✉',
        'narrative': '{name}写下一句给明年此刻的话，把纸条折好压在星灯纹样的信封里。',
    },
}

_GUEST_ROLE_LABELS = {'你的冒险者', '同行队友', '同席来客'}


def _utc_datetime(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _local_today(now: datetime | None = None) -> date:
    return _utc_datetime(now).astimezone(BEIJING_TZ).date()


def _naive_utc(now: datetime | None = None) -> datetime:
    return _utc_datetime(now).replace(tzinfo=None)


def _attendance_local_date(attended_at: datetime | None) -> date | None:
    if attended_at is None:
        return None
    if attended_at.tzinfo is None:
        attended_at = attended_at.replace(tzinfo=timezone.utc)
    return attended_at.astimezone(BEIJING_TZ).date()


def _local_day_utc_bounds(value: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(value, time.min, tzinfo=BEIJING_TZ)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_start, utc_start + timedelta(days=1)


def _config_int(key: str, *, minimum: int = 0) -> int:
    return max(minimum, int(current_app.config[key]))


def _normalize_pagination(
    page,
    per_page,
    *,
    default_key: str,
    maximum_key: str,
) -> tuple[int, int]:
    default_per_page = _config_int(default_key, minimum=1)
    maximum_per_page = _config_int(maximum_key, minimum=1)
    try:
        normalized_page = max(1, int(page or 1))
    except (TypeError, ValueError):
        normalized_page = 1
    try:
        normalized_per_page = int(per_page or default_per_page)
    except (TypeError, ValueError):
        normalized_per_page = default_per_page
    normalized_per_page = min(maximum_per_page, max(1, normalized_per_page))
    return normalized_page, normalized_per_page


def _page_navigation_projection(total: int, page: int, per_page: int) -> dict:
    pages = max(1, (int(total) + int(per_page) - 1) // int(per_page))
    return {
        'page': page,
        'pages': pages,
        'has_next': page < pages,
        'has_prev': page > 1,
    }


def _visit_pagination_projection(total: int, page: int, per_page: int) -> dict:
    return {
        **_page_navigation_projection(total, page, per_page),
        'total': int(total),
    }


def _normalize_birthday(month, day) -> tuple[int | None, int | None, str | None]:
    if isinstance(month, bool) or isinstance(day, bool):
        return None, None, '生日月份和日期必须为整数'
    if isinstance(month, float) and not month.is_integer():
        return None, None, '生日月份和日期必须为整数'
    if isinstance(day, float) and not day.is_integer():
        return None, None, '生日月份和日期必须为整数'
    try:
        normalized_month = int(month)
        normalized_day = int(day)
        # 2000 是闰年，因此 2 月 29 日也能作为合法生日登记。
        date(2000, normalized_month, normalized_day)
    except (TypeError, ValueError):
        return None, None, '请输入有效的生日月份和日期'
    return normalized_month, normalized_day, None


def _birthday_occurrence(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return date(year, 2, 28)
        raise


def _next_birthday(today: date, month: int, day: int) -> date:
    this_year = _birthday_occurrence(today.year, month, day)
    if this_year >= today:
        return this_year
    return _birthday_occurrence(today.year + 1, month, day)


def _birthday_label(month: int, day: int) -> str:
    return f'{month}月{day}日'


def _date_label(value: date) -> str:
    return f'{value.year}年{value.month}月{value.day}日'


def _wish_projection(wish_key: str) -> dict:
    option = _WISH_OPTIONS[wish_key]
    return {
        'key': wish_key,
        'label': option['label'],
        'verse': option['verse'],
        'sigil': option['sigil'],
    }


def _wish_display_projection(wish_key: str) -> dict:
    option = _WISH_OPTIONS[wish_key]
    return {
        'label': option['label'],
        'verse': option['verse'],
        'sigil': option['sigil'],
    }


def _visit_action_projection(action_key: str) -> dict:
    option = _VISIT_ACTIONS[action_key]
    group_key = option['group_key']
    group = _VISIT_ACTION_GROUPS[group_key]
    return {
        'key': action_key,
        'label': option['label'],
        'description': option['description'],
        'sigil': option['sigil'],
        'group': {
            'key': group_key,
            'label': group['label'],
            'description': group['description'],
            'sigil': group['sigil'],
        },
    }


def _visit_moment_projection(action_key: str) -> dict:
    option = _VISIT_ACTIONS[action_key]
    group = _VISIT_ACTION_GROUPS[option['group_key']]
    return {
        'label': option['label'],
        'sigil': option['sigil'],
        'group_sigil': group['sigil'],
    }


def _visit_policy_projection() -> dict:
    return {
        'message_max_length': _config_int(
            'ADVENTURER_S1_CEREMONY_VISIT_MESSAGE_MAX_LENGTH',
            minimum=1,
        ),
    }


def _feast_projection(snapshot=None) -> list[dict]:
    default_source = _FEAST_THEMES[_DEFAULT_FEAST_KEY]['dishes']
    if isinstance(snapshot, dict):
        source = snapshot.get('dishes')
    elif isinstance(snapshot, (list, tuple)):
        source = snapshot
    else:
        source = default_source
    if not isinstance(source, (list, tuple)):
        source = ()
    feast: list[dict] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        key = str(item.get('key') or '').strip()[:40]
        name = str(item.get('name') or '').strip()[:100]
        if not key or not name:
            continue
        feast.append({
            'key': key,
            'course_label': str(item.get('course_label') or '宴席菜肴').strip()[:24],
            'name': name,
            'description': str(item.get('description') or '').strip()[:240],
            'sigil': str(item.get('sigil') or '◇').strip()[:4],
        })
    if feast or source is default_source:
        return feast
    return _feast_projection(default_source)


def _feast_theme_display_projection(feast_key: str, source: dict) -> dict:
    fallback = _FEAST_THEMES[_DEFAULT_FEAST_KEY]
    return {
        'key': feast_key if feast_key in _FEAST_THEMES else _DEFAULT_FEAST_KEY,
        'label': str(source.get('label') or fallback['label']).strip()[:60],
        'subtitle': str(source.get('subtitle') or fallback['subtitle']).strip()[:120],
        'description': str(
            source.get('description') or fallback['description']
        ).strip()[:240],
        'sigil': str(source.get('sigil') or fallback['sigil']).strip()[:4],
    }


def _feast_theme_option_projection(feast_key: str) -> dict:
    theme = _FEAST_THEMES[feast_key]
    return {
        **_feast_theme_display_projection(feast_key, theme),
        'dishes': _feast_projection(theme['dishes']),
    }


def _feast_theme_options_projection() -> list[dict]:
    return [
        _feast_theme_option_projection(feast_key)
        for feast_key in _FEAST_THEMES
    ]


def _build_feast_snapshot(feast_key: str) -> dict:
    return _feast_theme_option_projection(feast_key)


def _feast_theme_projection(snapshot=None, *, include_description=False) -> dict:
    if isinstance(snapshot, dict):
        feast_key = str(snapshot.get('key') or '').strip()
        resolved = _feast_theme_display_projection(feast_key, snapshot)
    else:
        resolved = None
        snapshot_keys = [item['key'] for item in _feast_projection(snapshot)]
        for feast_key, theme in _FEAST_THEMES.items():
            theme_keys = [item['key'] for item in _feast_projection(theme['dishes'])]
            if snapshot_keys == theme_keys:
                resolved = _feast_theme_display_projection(feast_key, theme)
                break
        if resolved is None:
            default_theme = _FEAST_THEMES[_DEFAULT_FEAST_KEY]
            resolved = _feast_theme_display_projection(
                _DEFAULT_FEAST_KEY,
                default_theme,
            )
    projection = {
        'label': resolved['label'],
        'subtitle': resolved['subtitle'],
        'sigil': resolved['sigil'],
    }
    if include_description:
        projection['description'] = resolved['description']
    return projection


def _feast_table_projection(snapshot=None) -> list[dict]:
    return [
        {
            'key': item['key'],
            'name': item['name'],
            'sigil': item['sigil'],
        }
        for item in _feast_projection(snapshot)
    ]


def _feast_receipt_projection(snapshot=None) -> list[dict]:
    return [
        {
            'key': item['key'],
            'course_label': item['course_label'],
            'name': item['name'],
            'sigil': item['sigil'],
        }
        for item in _feast_projection(snapshot)
    ]


def _legacy_host_adventurer_name(snapshot) -> str | None:
    if not isinstance(snapshot, list):
        return None
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        if str(item.get('role_label') or '').strip() != '你的冒险者':
            continue
        name = str(item.get('name') or '').strip()[:100]
        if name:
            return name
    return None


def _host_projection(snapshot, celebrant_name: str, legacy_guests=None) -> dict:
    player_name = str(celebrant_name or '今夜寿星').strip()[:64] or '今夜寿星'
    adventurer_name = None
    if isinstance(snapshot, dict):
        frozen_player_name = str(snapshot.get('player_name') or '').strip()[:64]
        if frozen_player_name:
            player_name = frozen_player_name
        adventurer_name = str(snapshot.get('adventurer_name') or '').strip()[:100] or None
    if not adventurer_name:
        adventurer_name = _legacy_host_adventurer_name(legacy_guests)
    return {
        'display_name': adventurer_name or player_name,
        'player_name': player_name,
        'adventurer_name': adventurer_name,
        'role_label': '寿星主位',
    }


def _host_identity_projection(snapshot, celebrant_name: str, legacy_guests=None) -> dict:
    host = _host_projection(snapshot, celebrant_name, legacy_guests)
    return {
        'display_name': host['display_name'],
        'player_name': host['player_name'],
        'adventurer_name': host['adventurer_name'],
    }


def _guest_projection(snapshot) -> list[dict]:
    if not isinstance(snapshot, list):
        return []
    guests: list[dict] = []
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()[:100]
        if not name:
            continue
        role_label = str(item.get('role_label') or '').strip()
        # 旧礼帖曾把自己的冒险者同时写进来宾席；读取时归并到寿星主位。
        if role_label == '你的冒险者':
            continue
        if role_label not in _GUEST_ROLE_LABELS:
            role_label = '同席来客'
        guests.append({'name': name, 'role_label': role_label})
    return guests


def _build_host_snapshot(celebrant_name: str, context: dict | None) -> dict:
    owned_id = (context or {}).get('owned_adventurer_id')
    members = (context or {}).get('members')
    adventurer_name = None
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict) or member.get('adventurer_id') != owned_id:
                continue
            adventurer_name = str(member.get('name') or '').strip()[:100] or None
            break
    return {
        'player_name': str(celebrant_name or '').strip()[:64],
        'adventurer_name': adventurer_name,
    }


def _build_guest_snapshot(context: dict | None) -> list[dict]:
    if not context:
        return []
    owned_id = context.get('owned_adventurer_id')
    members = context.get('members') if isinstance(context.get('members'), list) else []
    guests = []
    seen_ids = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        adventurer_id = member.get('adventurer_id')
        name = str(member.get('name') or '').strip()[:100]
        if not name or adventurer_id == owned_id or adventurer_id in seen_ids:
            continue
        seen_ids.add(adventurer_id)
        guests.append({'name': name, 'role_label': '同行队友'})
    return guests


def _build_banquet_preview(context: dict | None, celebrant_name: str) -> dict:
    host_snapshot = _build_host_snapshot(celebrant_name, context)
    host = _host_projection(host_snapshot, celebrant_name)
    guests = _build_guest_snapshot(context)
    party_name = str((context or {}).get('party_name') or '').strip()[:100]
    if party_name:
        source_label = f'当前小队 · {party_name}'
    elif host['adventurer_name']:
        source_label = '寿星主位已经铺好桌巾'
    else:
        source_label = '酒馆侍者为寿星守席'
    if guests:
        invitation_copy = (
            f'{host["display_name"]}只占寿星主位这一席；礼堂还向当前小队的 '
            f'{len(guests)} 位同行者递出了请帖。'
        )
    elif host['adventurer_name']:
        invitation_copy = (
            f'{host["display_name"]}就是你在酒馆里的寿星身影，不会再重复占一张来宾席。'
            '今夜没有同行队友也没关系，酒馆会把长桌照常摆满。'
        )
    else:
        invitation_copy = (
            '寿星主位只留一席。今夜没有冒险者能赶来，酒馆仍会把长桌照常摆满。'
        )
    return {
        'source_label': source_label,
        'invitation_copy': invitation_copy,
        'host': host,
        'guests': guests,
        'guest_count': len(guests),
        'feast_options': _feast_theme_options_projection(),
        'host_note': '待你选定宴席与烛愿，掌柜会代表整间酒馆举杯；是否欢迎其他玩家串门，由你决定。',
    }


def _build_tavern_blessing(wish_key: str, celebrant_name: str) -> str:
    template = _TAVERN_BLESSINGS.get(wish_key, _TAVERN_BLESSINGS['homefire'])
    return template.format(name=celebrant_name)


def _birthday_gift_projection(attendance, *, can_claim: bool) -> dict:
    delivered = bool(attendance.gift_claimed_at)
    if delivered:
        destination_label = {
            'equipment': '随身行囊',
            'warehouse': '仓库',
        }.get(attendance.gift_destination, '随身行囊')
        status = 'delivered'
        status_label = f'掌柜已将礼盒送入{destination_label}'
    elif can_claim:
        destination_label = None
        status = 'claimable'
        status_label = '礼盒已经系好丝带，正等你收下'
    else:
        destination_label = None
        status = 'waiting_for_adventurer'
        status_label = '酒馆暂代保管；拥有冒险者后即可领取'
    return {
        'name': BIRTHDAY_CAKE_ITEM_NAME,
        'description': BIRTHDAY_CAKE_ITEM_DESCRIPTION,
        'sigil': '✦',
        'rarity_label': '稀有纪念赠礼',
        'immutable_label': '酒馆封存 · 名称与故事不可改写',
        'status': status,
        'status_label': status_label,
        'destination_label': destination_label,
        'can_claim': bool(can_claim and not delivered),
    }


def _deliver_birthday_gift(
    attendance,
    user_id: int,
    *,
    delivered_at: datetime,
) -> str | None:
    """Grant once per locked attendance and persist its durable annual receipt."""
    if attendance.gift_claimed_at is not None:
        return None
    adventurer = lock_owned_adventurer(user_id)
    if adventurer is None:
        return '请先让一位属于你的冒险者在酒馆落脚，再来领取蛋糕'
    _, grant_error = grant_catalog_item(
        adventurer,
        BIRTHDAY_CAKE_ITEM_NAME,
        prefer_equipment=True,
        source_type=BIRTHDAY_GIFT_SOURCE_TYPE,
        description=f'星灯礼堂送上今年的生日赠礼「{BIRTHDAY_CAKE_ITEM_NAME}」',
        related_id=str(attendance.id),
        related_type=BIRTHDAY_GIFT_RELATED_TYPE,
    )
    if grant_error:
        return grant_error
    gift_log = get_ceremony_gift_log(
        attendance.id,
        item_name=BIRTHDAY_CAKE_ITEM_NAME,
        source_type=BIRTHDAY_GIFT_SOURCE_TYPE,
        related_type=BIRTHDAY_GIFT_RELATED_TYPE,
    )
    if gift_log is None:
        raise DatabaseError(
            'S1 birthday cake grant log was not persisted',
            operation='grant_birthday_cake',
            model='AdventurerS1ItemLog',
        )
    mark_attendance_gift_delivered(
        attendance,
        adventurer_id=adventurer.id,
        destination=(
            'warehouse' if gift_log.change_type == 'overflow' else 'equipment'
        ),
        claimed_at=delivered_at,
    )
    return None


def _serialize_visit(visit) -> dict:
    action_key = (
        visit.action_key
        if visit.action_key in _VISIT_ACTIONS
        else 'pull_up_chair'
    )
    visitor_name = str(visit.visitor_name or '一位旅人').strip()[:64] or '一位旅人'
    action = _VISIT_ACTIONS[action_key]
    return {
        'action': _visit_moment_projection(action_key),
        'narrative': action['narrative'].format(name=visitor_name),
        'message': str(visit.message or '').strip()[:160] or None,
        'created_at': to_utc_iso(visit.created_at),
    }


def _serialize_open_table(attendance, viewer_user_id: int, visitor_count: int) -> dict:
    wish_key = (
        attendance.wish_key
        if attendance.wish_key in _WISH_OPTIONS
        else 'homefire'
    )
    guests = _guest_projection(attendance.guest_snapshot)
    return {
        'token': attendance.visit_token,
        'host': _host_identity_projection(
            attendance.host_snapshot,
            attendance.celebrant_name,
            attendance.guest_snapshot,
        ),
        'wish': _wish_display_projection(wish_key),
        'companion_count': len(guests),
        'visitor_count': int(visitor_count),
        'feast_theme': _feast_theme_projection(attendance.feast_snapshot),
        'feast': _feast_table_projection(attendance.feast_snapshot),
        'is_mine': int(attendance.user_id) == int(viewer_user_id),
    }


def _serialize_receipt(
    attendance,
    today: date,
    visitor_count: int,
    birthday_gift: dict | None = None,
) -> dict:
    wish_key = (
        attendance.wish_key
        if attendance.wish_key in _WISH_OPTIONS
        else 'homefire'
    )
    guests = _guest_projection(attendance.guest_snapshot)
    feast = _feast_receipt_projection(attendance.feast_snapshot)
    tavern_blessing = str(attendance.tavern_blessing or '').strip()[:800]
    if not tavern_blessing:
        tavern_blessing = _build_tavern_blessing(
            wish_key,
            attendance.celebrant_name,
        )
    can_manage_visitors = _attendance_local_date(attendance.attended_at) == today
    return {
        'year': attendance.occurrence_year,
        'celebrant_name': attendance.celebrant_name,
        'host': _host_identity_projection(
            attendance.host_snapshot,
            attendance.celebrant_name,
            attendance.guest_snapshot,
        ),
        'birthday': {
            'label': _birthday_label(
                attendance.birthday_month,
                attendance.birthday_day,
            ),
        },
        'wish': _wish_display_projection(wish_key),
        'guests': guests,
        'guest_count': len(guests),
        'feast_theme': _feast_theme_projection(
            attendance.feast_snapshot,
            include_description=True,
        ),
        'feast': feast,
        'tavern_blessing': tavern_blessing,
        'gift': birthday_gift or _birthday_gift_projection(
            attendance,
            can_claim=False,
        ),
        'open_table': {
            'token': attendance.visit_token,
            'is_open': bool(attendance.visitors_open and can_manage_visitors),
            'can_manage': can_manage_visitors,
            'visitor_count': int(visitor_count),
        },
        'attended_at': to_utc_iso(attendance.attended_at),
    }


def _build_state(
    username: str,
    nickname: str | None,
    profile,
    attendance,
    today: date,
    banquet_context: dict | None = None,
    visitor_count: int = 0,
    birthday_gift: dict | None = None,
) -> dict:
    hall = {
        'name': '星灯礼堂',
        'eyebrow': '酒馆二层 · 长夜尽头的灯火',
        'description': '酒馆把值得纪念的日子写进灯火。今夜亮着的生日桌，可以选择向所有旅人敞开。',
        'local_date_label': _date_label(today),
        'timezone_label': '酒馆时刻 · UTC+8',
    }
    celebrant_name = str(nickname or username)
    receipt = (
        _serialize_receipt(
            attendance,
            today,
            visitor_count,
            birthday_gift,
        )
        if attendance
        else None
    )
    banquet = (
        _build_banquet_preview(banquet_context, celebrant_name)
        if receipt is None and banquet_context is not None
        else None
    )
    base_event = {
        'title': '星灯生日宴',
        'eyebrow': 'THE OPEN HOUSE OF STARLIGHT',
        'description': '寿星与同行者先在长桌落座，也可以把门牌翻到“欢迎串门”，让今夜的玩家来坐一会儿、吃点东西、说几句话。',
        'rules': [
            '只登记月与日，不记录出生年份',
            '每个账号每个自然年只落印一份生日礼帖',
            '寿星与自己的冒险者合为一张主位，不重复占席',
            '同一天可以有多张生日桌；只有寿星主动开放的桌子会出现在大厅',
            '串门、菜肴、烛愿与祝词只留下文字、不改变数值；生日蛋糕是每年一份的独立纪念赠礼',
        ],
        'wish_options': [_wish_projection(key) for key in _WISH_OPTIONS],
        'visit_actions': [_visit_action_projection(key) for key in _VISIT_ACTIONS],
        'visit_policy': _visit_policy_projection(),
        'banquet': banquet,
        'receipt': receipt,
    }
    if profile is None:
        event = {
            **base_event,
            'status': 'needs_birthday',
            'status_label': '等待登记',
            'birthday': None,
            'observed_date_label': None,
            'days_until': None,
        }
    else:
        observed_this_year = _birthday_occurrence(
            today.year,
            profile.birthday_month,
            profile.birthday_day,
        )
        next_birthday = _next_birthday(
            today,
            profile.birthday_month,
            profile.birthday_day,
        )
        if attendance is not None:
            status = 'celebrated'
            status_label = '礼帖已落印'
        elif observed_this_year == today:
            status = 'available'
            status_label = '今夜开席'
        else:
            status = 'waiting'
            status_label = '静候灯亮'
        event = {
            **base_event,
            'status': status,
            'status_label': status_label,
            'birthday': {
                'month': profile.birthday_month,
                'day': profile.birthday_day,
                'label': _birthday_label(
                    profile.birthday_month,
                    profile.birthday_day,
                ),
                'can_edit': attendance is None,
            },
            'observed_date_label': _date_label(next_birthday),
            'days_until': (next_birthday - today).days,
        }
    return {
        'hall': hall,
        'celebrant_name': celebrant_name,
        'birthday_event': event,
    }


class S1CeremonyHallService:
    """Own birthday profiles, annual receipts, and opt-in social visits."""

    @staticmethod
    def get_spectator_state(
        display_name: str | None,
        *,
        now: datetime | None = None,
    ) -> tuple[dict, None]:
        """Project public hall metadata without requiring a persisted player."""
        today = _local_today(_utc_datetime(now))
        state = _build_state(
            str(display_name or '访客'),
            None,
            None,
            None,
            today,
        )
        state['birthday_event'].update({
            'status': 'spectator',
            'status_label': '跨区庆生访客',
            'birthday': None,
            'observed_date': None,
            'observed_date_label': None,
            'days_until': None,
            'can_attend': False,
            'banquet': None,
            'receipt': None,
        })
        return state, None

    @staticmethod
    def get_state(
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        row = get_player_state(
            user_id,
            ceremony_type=BIRTHDAY_CEREMONY_TYPE,
            occurrence_year=today.year,
        )
        if row is None:
            return None, '未找到当前玩家'
        username, nickname, profile, attendance = row
        banquet_context = None
        visitor_count = count_visits(attendance.id) if attendance is not None else 0
        birthday_gift = None
        if attendance is not None:
            can_claim = bool(
                attendance.gift_claimed_at is None
                and get_owned_adventurer_name(user_id) is not None
            )
            birthday_gift = _birthday_gift_projection(
                attendance,
                can_claim=can_claim,
            )
        if profile is not None and attendance is None:
            birthday_today = _birthday_occurrence(
                today.year,
                profile.birthday_month,
                profile.birthday_day,
            )
            if birthday_today == today:
                banquet_context = get_banquet_companion_context(user_id)
        return _build_state(
            username,
            nickname,
            profile,
            attendance,
            today,
            banquet_context,
            visitor_count,
            birthday_gift,
        ), None

    @staticmethod
    def set_birthday(
        user_id: int,
        month,
        day,
        *,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        birthday_month, birthday_day, validation_error = _normalize_birthday(
            month,
            day,
        )
        if validation_error:
            return None, validation_error
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        try:
            with db_manager.transaction():
                user = lock_user(user_id)
                if user is None:
                    return None, '未找到当前玩家'
                profile = get_profile(user_id)
                attendance = get_attendance(
                    user_id,
                    ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                    occurrence_year=today.year,
                )
                if attendance is not None:
                    if (
                        profile is None
                        or profile.birthday_month != birthday_month
                        or profile.birthday_day != birthday_day
                    ):
                        return None, '本年度生日礼帖已经落印，生日将在下一年重新开放修改'
                    return S1CeremonyHallService.get_state(
                        user_id,
                        now=current_time,
                    )
                save_birthday_profile(
                    profile,
                    user_id,
                    birthday_month=birthday_month,
                    birthday_day=birthday_day,
                )
        except DatabaseError:
            logger.exception('Failed to save S1 ceremony birthday profile')
            return None, '星灯礼簿暂时无法落笔，请稍后再试'
        return S1CeremonyHallService.get_state(user_id, now=current_time)

    @staticmethod
    def attend_birthday(
        user_id: int,
        wish_key,
        visitors_open=False,
        *,
        feast_key=None,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        normalized_wish = str(wish_key or '').strip()
        if normalized_wish not in _WISH_OPTIONS:
            return None, '请选择一枚有效的烛愿印记'
        normalized_feast = str(feast_key or _DEFAULT_FEAST_KEY).strip()
        if normalized_feast not in _FEAST_THEMES:
            return None, '请选择一席仍在酒馆菜单上的生日宴'
        if type(visitors_open) is not bool:
            return None, '串门门牌状态必须为布尔值'
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        attended_at = _naive_utc(current_time)
        try:
            with db_manager.transaction():
                user = lock_user(user_id)
                if user is None:
                    return None, '未找到当前玩家'
                attendance = get_attendance(
                    user_id,
                    ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                    occurrence_year=today.year,
                )
                if attendance is not None:
                    return S1CeremonyHallService.get_state(
                        user_id,
                        now=current_time,
                    )
                profile = get_profile(user_id)
                if profile is None:
                    return None, '请先在星灯礼簿登记生日'
                birthday_today = _birthday_occurrence(
                    today.year,
                    profile.birthday_month,
                    profile.birthday_day,
                )
                if birthday_today != today:
                    return None, '礼堂尚未为你点亮今夜寿烛'
                celebrant_name = str(user.nickname or user.username).strip()[:64]
                banquet_context = get_banquet_companion_context(user_id)
                host_snapshot = _build_host_snapshot(
                    celebrant_name,
                    banquet_context,
                )
                guest_snapshot = _build_guest_snapshot(banquet_context)
                feast_snapshot = _build_feast_snapshot(normalized_feast)
                attendance = create_attendance(
                    user_id=user_id,
                    ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                    occurrence_year=today.year,
                    birthday_month=profile.birthday_month,
                    birthday_day=profile.birthday_day,
                    wish_key=normalized_wish,
                    celebrant_name=celebrant_name,
                    host_snapshot=host_snapshot,
                    guest_snapshot=guest_snapshot,
                    feast_snapshot=feast_snapshot,
                    tavern_blessing=_build_tavern_blessing(
                        normalized_wish,
                        celebrant_name,
                    ),
                    visit_token=uuid4().hex,
                    visitors_open=visitors_open,
                    visitors_opened_at=attended_at if visitors_open else None,
                    attended_at=attended_at,
                )
                gift_error = _deliver_birthday_gift(
                    attendance,
                    user_id,
                    delivered_at=attended_at,
                )
                if gift_error:
                    logger.info(
                        'Deferred S1 birthday cake delivery attendance_id=%s: %s',
                        attendance.id,
                        gift_error,
                    )
        except DatabaseError:
            logger.exception('Failed to create S1 birthday ceremony attendance')
            return None, '星灯礼帖暂时无法落印，请稍后再试'
        return S1CeremonyHallService.get_state(user_id, now=current_time)

    @staticmethod
    def claim_birthday_gift(
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        try:
            with db_manager.transaction():
                user = lock_user(user_id)
                if user is None:
                    return None, '未找到当前玩家'
                attendance = get_attendance(
                    user_id,
                    ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                    occurrence_year=today.year,
                    for_update=True,
                )
                if attendance is None:
                    return None, '本年度生日礼帖尚未落印，蛋糕还没有装盒'
                gift_error = _deliver_birthday_gift(
                    attendance,
                    user_id,
                    delivered_at=_naive_utc(current_time),
                )
                if gift_error:
                    return None, f'蛋糕礼盒暂时还在柜台等你：{gift_error}'
        except DatabaseError:
            logger.exception('Failed to grant S1 birthday ceremony cake')
            return None, '蛋糕礼盒暂时没有递到手中，请稍后再试'
        return S1CeremonyHallService.get_state(user_id, now=current_time)

    @staticmethod
    def set_visitors_open(
        user_id: int,
        visitors_open,
        *,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        if type(visitors_open) is not bool:
            return None, '串门门牌状态必须为布尔值'
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        opened_at = _naive_utc(current_time)
        try:
            with db_manager.transaction():
                if lock_user(user_id) is None:
                    return None, '未找到当前玩家'
                attendance = get_attendance(
                    user_id,
                    ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                    occurrence_year=today.year,
                    for_update=True,
                )
                if attendance is None:
                    return None, '请先完成今夜的生日仪式'
                if _attendance_local_date(attendance.attended_at) != today:
                    return None, '只有生日开席当晚可以翻动串门门牌'
                set_attendance_visitors_open(
                    attendance,
                    visitors_open=visitors_open,
                    visit_token=attendance.visit_token or uuid4().hex,
                    opened_at=opened_at,
                )
        except DatabaseError:
            logger.exception('Failed to update S1 birthday table visibility')
            return None, '礼堂门牌暂时没有翻动，请稍后再试'
        return S1CeremonyHallService.get_state(user_id, now=current_time)

    @staticmethod
    def list_open_tables(
        user_id: int,
        *,
        page=None,
        per_page=None,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        normalized_page, normalized_per_page = _normalize_pagination(
            page,
            per_page,
            default_key='ADVENTURER_S1_CEREMONY_OPEN_TABLE_PAGE_SIZE',
            maximum_key='ADVENTURER_S1_CEREMONY_OPEN_TABLE_PAGE_SIZE_MAX',
        )
        day_start_utc, day_end_utc = _local_day_utc_bounds(today)
        rows, total = list_open_birthday_tables(
            ceremony_type=BIRTHDAY_CEREMONY_TYPE,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
            page=normalized_page,
            per_page=normalized_per_page,
        )
        pagination = _page_navigation_projection(
            total,
            normalized_page,
            normalized_per_page,
        )
        if normalized_page > pagination['pages']:
            normalized_page = pagination['pages']
            rows, total = list_open_birthday_tables(
                ceremony_type=BIRTHDAY_CEREMONY_TYPE,
                day_start_utc=day_start_utc,
                day_end_utc=day_end_utc,
                page=normalized_page,
                per_page=normalized_per_page,
            )
            pagination = _page_navigation_projection(
                total,
                normalized_page,
                normalized_per_page,
            )
        return {
            'items': [
                _serialize_open_table(attendance, user_id, visitor_count)
                for attendance, visitor_count in rows
            ],
            'pagination': pagination,
            'visit_actions': [
                _visit_action_projection(key)
                for key in _VISIT_ACTIONS
            ],
        }, None

    @staticmethod
    def list_table_visits(
        user_id: int,
        visit_token,
        *,
        page=None,
        per_page=None,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        normalized_token = str(visit_token or '').strip()
        if len(normalized_token) != 32:
            return None, '没有找到这张生日桌'
        attendance = get_attendance_by_visit_token(normalized_token)
        if attendance is None:
            return None, '没有找到这张生日桌'
        today = _local_today(now)
        is_owner = int(attendance.user_id) == int(user_id)
        is_open_today = bool(
            attendance.visitors_open
            and _attendance_local_date(attendance.attended_at) == today
        )
        if not is_owner and not is_open_today:
            return None, '这张生日桌的门牌已经翻回“请勿打扰”'
        normalized_page, normalized_per_page = _normalize_pagination(
            page,
            per_page,
            default_key='ADVENTURER_S1_CEREMONY_VISIT_PAGE_SIZE',
            maximum_key='ADVENTURER_S1_CEREMONY_VISIT_PAGE_SIZE_MAX',
        )
        rows, total = list_visits(
            attendance.id,
            page=normalized_page,
            per_page=normalized_per_page,
        )
        pagination = _visit_pagination_projection(
            total,
            normalized_page,
            normalized_per_page,
        )
        if normalized_page > pagination['pages']:
            normalized_page = pagination['pages']
            rows, total = list_visits(
                attendance.id,
                page=normalized_page,
                per_page=normalized_per_page,
            )
            pagination = _visit_pagination_projection(
                total,
                normalized_page,
                normalized_per_page,
            )
        return {
            'table': _serialize_open_table(attendance, user_id, total),
            'items': [_serialize_visit(row) for row in rows],
            'pagination': pagination,
            'visit_actions': [
                _visit_action_projection(key)
                for key in _VISIT_ACTIONS
            ],
        }, None

    @staticmethod
    def visit_table(
        user_id: int,
        visit_token,
        action_key,
        message,
        request_token,
        *,
        now: datetime | None = None,
    ) -> tuple[dict | None, str | None]:
        normalized_token = str(visit_token or '').strip()
        if len(normalized_token) != 32:
            return None, '没有找到这张生日桌'
        normalized_action = str(action_key or '').strip()
        if normalized_action not in _VISIT_ACTIONS:
            return None, '请选择一种串门方式'
        normalized_request_token = str(request_token or '').strip()
        if len(normalized_request_token) != 32:
            return None, '这次串门凭据无效，请重新操作'
        if message is not None and not isinstance(message, str):
            return None, '祝福留言必须是文字'
        normalized_message = str(message or '').strip()
        message_max_length = _config_int(
            'ADVENTURER_S1_CEREMONY_VISIT_MESSAGE_MAX_LENGTH',
            minimum=1,
        )
        if len(normalized_message) > message_max_length:
            return None, f'祝福留言不能超过 {message_max_length} 字'
        current_time = _utc_datetime(now)
        today = _local_today(current_time)
        created_at = _naive_utc(current_time)
        cooldown_seconds = _config_int(
            'ADVENTURER_S1_CEREMONY_VISIT_COOLDOWN_SECONDS',
            minimum=0,
        )
        daily_limit = _config_int(
            'ADVENTURER_S1_CEREMONY_VISIT_DAILY_LIMIT_PER_TABLE',
            minimum=1,
        )
        visit_payload = None
        try:
            with db_manager.transaction():
                visitor = lock_user(user_id)
                if visitor is None:
                    return None, '未找到当前玩家'
                attendance = get_attendance_by_visit_token(
                    normalized_token,
                    for_update=True,
                )
                if attendance is None:
                    return None, '没有找到这张生日桌'
                if int(attendance.user_id) == int(user_id):
                    return None, '这是你的生日桌，留在主位招呼来客就好'
                existing_visit = get_visit_by_request_token(
                    attendance.id,
                    user_id,
                    normalized_request_token,
                )
                if existing_visit is not None:
                    return _serialize_visit(existing_visit), None
                if not attendance.visitors_open:
                    return None, '这张生日桌的门牌已经翻回“请勿打扰”'
                if _attendance_local_date(attendance.attended_at) != today:
                    return None, '这张生日桌已经随昨夜的灯火收起'
                visit_count = count_visits_for_user(attendance.id, user_id)
                if visit_count >= daily_limit:
                    return None, '你已经在这张桌边留下很多片刻了，今晚先把位置让给其他旅人吧'
                latest_visit = get_latest_visit_for_user(attendance.id, user_id)
                if cooldown_seconds and latest_visit and latest_visit.created_at:
                    elapsed = (created_at - latest_visit.created_at).total_seconds()
                    if elapsed < cooldown_seconds:
                        remaining = int(cooldown_seconds - elapsed) + 1
                        return None, f'先在桌边坐一会儿吧，{remaining} 秒后还能继续互动'
                visitor_name = get_owned_adventurer_name(user_id) or (
                    str(visitor.nickname or visitor.username).strip()[:64]
                    or '一位旅人'
                )
                visit = create_visit(
                    attendance_id=attendance.id,
                    visitor_user_id=user_id,
                    visitor_name=visitor_name,
                    action_key=normalized_action,
                    message=normalized_message or None,
                    request_token=normalized_request_token,
                    created_at=created_at,
                )
                visit_payload = _serialize_visit(visit)
        except DatabaseError:
            logger.exception('Failed to create S1 birthday table visit')
            return None, '这段串门片刻暂时没有写进礼簿，请稍后再试'
        return visit_payload, None
