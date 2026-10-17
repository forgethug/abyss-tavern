"""深渊温泉环境闲聊：预生成文案池 + 按 tick 概率拼装。

照抄深渊茶舍闲聊系统，主题换成「泡汤」。入座冒险者会按时间桶（tick）有概率
冒出一句闲聊，可引用同池在座冒险者的名字；若两人有好感度记录，会按亲疏档位
挑选更贴切的台词。纯静态模板，零 AI token；好感度分值不向玩家暴露。

占位符：
- {speaker}  当前座位上的冒险者
- {target}   对话对象（同池在座者）
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from flask import current_app

from plugins.adventurer_s1.models import AdventurerS1Relationship, AdventurerS1HotSpringSeat

# 复用酒馆的好感度工具函数
from plugins.adventurer_s1.services.adventurer_s1_tavern_chatter_service import (
    _relation_bucket,
    _relation_matches,
    _pair_key,
)

# scope: solo / same_table（同池才会互动）
# relation: any / close / friendly / neutral / strained / hostile / rival
S1_HOT_SPRING_CHATTER_TEMPLATES: List[dict] = [
    # ── 独白 ──────────────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}沉到温泉里，只露出一双眯起的眼睛，舒服地哼了一声。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}靠在池壁上，任热气把肩上的伤痛一点点泡软。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用热水拍了拍脸，把这些天的疲惫往下压了压。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}闭目仰躺，水面的雾气在头顶慢慢散开。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}伸了个长长的懒腰，骨节发出轻微的响声。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}深吸一口带着硫磺味的热气，长长地呼了出去。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着自己泡得发红的手指，露出一丝困意。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把头巾打湿盖在额头上，整个人懒懒地沉进水里。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用脚趾搅着水面的倒影，像在数还剩多少烦恼。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}舀起一捧热水浇在肩上，旧伤的酸痛缓缓退去。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}打了个小盹，差点滑进水里，惊醒后不好意思地环顾四周。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把毛巾叠好搁在池沿，终于卸下了一身的紧绷。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}看着水汽在灯下浮动，喃喃说这才像活着。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}数着池底的鹅卵石，已经数到第三十七颗了。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把整张脸浸进水里，憋了几秒才浮上来，长出一口气。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}小声哼着不成调的歌，回声在雾里荡开。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}揉着泡软的肩膀，舒服得直叹气。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把冒险日志摊在池沿，墨迹却被水汽晕开了一角。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着水面升腾的雾，眼神渐渐放空。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}自言自语：「再泡一刻钟就回宿舍。」——一刻钟过去了，还在。'},
    # ── 同池 ──────────────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}朝{target}泼了点水：「醒醒，别泡睡着了。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「这水温正好吧？我特意挑的这个时辰。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}并排靠在池壁，谁都没说话，只听见水声。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}说：「泡完这场，明天又能下本了。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}指着自己的伤疤对{target}说：「这道，就是上回那只大家伙留的。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把温泉蛋递给{target}：「尝尝，泉眼里煨的。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}争论哪个池子水更烫，谁也说服不了谁。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}坦白：「其实我有点怕水，但这汤是真舒服。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}给{target}让出靠泉眼的好位置：「你伤重，你来这边。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}举起酒杯：「敬咱俩还能囫囵着泡在这儿。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}跟{target}回忆上次探险，笑得呛了一口热水。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}拍拍{target}的肩：「有你在后排，我敢往前多冲几步。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}低声对{target}说：「要是哪天我回不来，帮我把信交出去。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}帮{target}揉着泡软的旧伤：「别硬撑，疼就说一声。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}握住{target}的手腕：「答应我，下回别一个人冲太前。」'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}礼貌地问{target}：「介意我也泡这池吗？」其实早就下了水。'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}跟{target}点头致意，话停在「今天水汽真大」。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}默默挪到池子另一头，尽量离{target}远一点。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}瞥了眼{target}，冷笑：「没想到你也来这种地方放松。」'},
    {'scope': 'same_table', 'relation': 'hostile', 'text': '{speaker}冲{target}溅起一片水花：「看你自己那半边池子去。」'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}盯着{target}：「下回下本，各走各的，别拖后腿。」'},
    # ── 多句 / 对白感 ─────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}：「你信命运吗？」{target}：「信，但信它会给第二次机会。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}：「今天差点回不来。」{target}：「所以你在泡汤，我在听你还喘着气。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你那队还缺人吗？」{target}：「缺，但缺的是靠谱的。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}喊：「下回别抢我人头！」声音在雾里荡来荡去。'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}什么也没说，只是把毛巾分了一半搭到{target}肩上。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}跟{target}同池泡汤，却全程只跟自己的酒杯说话。'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}对{target}举杯：「敬你活着——也敬我活得比你久。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对着水里的倒影整理头发，又被自己泡红的脸逗笑了。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把酒杯搁在水面上，赌它能不能浮到对岸。'},
]


@dataclass
class _SeatCtx:
    seat: AdventurerS1HotSpringSeat
    adv_id: int
    adv_name: str


class AdventurerS1HotSpringChatterService:
    """温泉环境闲聊生成器。"""

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
        seats: Sequence[AdventurerS1HotSpringSeat],
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
            return [c for c in others if c.seat.table_key == speaker.seat.table_key]

        return []

    @staticmethod
    def _format_line(tpl: dict, speaker: _SeatCtx, target: Optional[_SeatCtx]) -> str:
        return tpl['text'].format(
            speaker=speaker.adv_name,
            target=target.adv_name if target else '',
        )

    @staticmethod
    def _pick_template_and_target(
        speaker: _SeatCtx,
        occupied: List[_SeatCtx],
        rel_map: Dict[Tuple[int, int], int],
        rng: random.Random,
    ) -> Optional[str]:
        viable: List[Tuple[dict, Optional[_SeatCtx]]] = []
        for tpl in S1_HOT_SPRING_CHATTER_TEMPLATES:
            scope = tpl.get('scope', 'solo')
            if scope == 'solo':
                if _relation_matches(tpl.get('relation', 'any'), 'neutral'):
                    viable.append((tpl, None))
                continue

            candidates = AdventurerS1HotSpringChatterService._candidates_for_scope(
                speaker, scope, occupied,
            )
            if not candidates:
                continue

            rng.shuffle(candidates)
            for cand in candidates:
                score = rel_map.get(_pair_key(speaker.adv_id, cand.adv_id))
                bucket = _relation_bucket(score)
                if _relation_matches(tpl.get('relation', 'any'), bucket):
                    viable.append((tpl, cand))
                    break

        if not viable:
            return None

        tpl, target = rng.choice(viable)
        return AdventurerS1HotSpringChatterService._format_line(tpl, speaker, target)

    @staticmethod
    def generate_for_seats(
        seats: Sequence[AdventurerS1HotSpringSeat],
        adv_name_map: Dict[int, str],
        stale_seat_ids: Iterable[int],
        now: Optional[datetime] = None,
    ) -> Dict[int, Optional[str]]:
        """为每个有效占座座位生成闲聊文案。返回 seat_id -> chatter（无则 None）。"""
        prob = float(current_app.config.get('ADVENTURER_S1_TAVERN_CHATTER_PROB', 0.35))
        prob = min(max(prob, 0.0), 1.0)
        bucket = AdventurerS1HotSpringChatterService._tick_bucket(now)

        occupied = AdventurerS1HotSpringChatterService._build_context(
            seats, adv_name_map, stale_seat_ids,
        )
        if not occupied:
            return {}

        rel_map = AdventurerS1HotSpringChatterService._load_relationship_map(
            [c.adv_id for c in occupied],
        )

        result: Dict[int, Optional[str]] = {}
        for ctx in occupied:
            rng = random.Random(f's1-hot-spring-chatter:{ctx.seat.id}:{ctx.adv_id}:{bucket}')
            if rng.random() > prob:
                result[ctx.seat.id] = None
                continue
            line = AdventurerS1HotSpringChatterService._pick_template_and_target(
                ctx, occupied, rel_map, rng,
            )
            result[ctx.seat.id] = line
        return result
