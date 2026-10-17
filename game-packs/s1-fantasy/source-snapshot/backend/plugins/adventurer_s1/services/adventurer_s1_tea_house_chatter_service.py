"""深渊茶舍环境闲聊：预生成文案池 + 按 tick 概率拼装。

入座冒险者会按时间桶（tick）有概率冒出一句闲聊，可引用同桌或另一桌
在座冒险者的名字；若两人有好感度记录，会按亲疏档位挑选更贴切的台词。
纯静态模板，零 AI token；好感度分值不向玩家暴露。

与酒馆闲聊系统独立：茶舍文案以「茶」为主题，不涉及酒饮。

占位符：
- {speaker}  当前座位上的冒险者
- {target}   对话对象（同桌 / 另一桌在座者）
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from flask import current_app

from plugins.adventurer_s1.models import AdventurerS1Relationship, AdventurerS1TeaHouseSeat

# 复用酒馆的好感度工具函数
from plugins.adventurer_s1.services.adventurer_s1_tavern_chatter_service import (
    _relation_bucket,
    _relation_matches,
    _pair_key,
)

# scope: solo / same_table（同桌才会互动）
# relation: any / close / friendly / neutral / strained / hostile / rival
S1_TEA_HOUSE_CHATTER_TEMPLATES: List[dict] = [
    # ── 独白 ──────────────────────────────────────────────────────────────
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}双手捧着茶杯，热气熏得眯起了眼。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}小口啜着茶，目光落在窗外的云上。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶盖掀了又合，像在等什么人。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}闭目养神，茶凉了也没察觉。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用指尖蘸茶水在桌上画了个圈，又擦掉了。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}深吸一口茶香，长长地呼出一口气。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶叶捞出来数了数，叹了口气。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}端着茶杯发呆，手指无意识地摩挲杯沿。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对着茶汤里的倒影整理了一下头发。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}轻轻吹开茶沫，小心翼翼地抿了一口。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶杯转了三圈，才找到最舒服的握法。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}闻了闻茶香，说这味道像老家后山的春天。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把披风叠好放在椅背上，终于放松了肩膀。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}盯着杯中沉浮的茶叶，像在看一场慢动作的舞蹈。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}打了个小盹，茶杯差点翻倒，惊醒后不好意思地环顾四周。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶壶往自己这边挪了挪，生怕被人抢走。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}用茶盖轻轻刮着杯沿，发出清脆的声响。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}忽然叹了口气，又端起茶杯喝了一口。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}数着窗外的雨滴，已经数到第三十七滴了。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把冒险日志摊在桌上，却只写下「今日无事」。'},
    # ── 同桌 ──────────────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}给{target}续了杯茶：「慢喝，不急。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你这杯是什么茶？闻着挺香。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}碰了碰茶杯，发出轻轻的瓷器声。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}说：「喝完这杯，明天又是一条好汉。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}指着窗外对{target}说：「雨停了，再坐会儿。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}把点心推给{target}：「配茶吃的，别客气。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}争论哪种茶更好喝，谁也说服不了谁。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}坦白：「其实我分不清红茶和绿茶。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}给{target}夹了块糕点：「你今天话少，不像你。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}举杯：「敬咱俩还能坐在这儿喝茶。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}跟{target}回忆上次探险，笑得差点呛到。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}拍拍{target}肩膀：「有你在，我敢多走一里路。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}低声对{target}说：「要是明天回不来，帮我把信交出去。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}把最后一口好茶让给{target}：「你比我更需要这杯。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}握住{target}的手腕：「答应我，别一个人冲太前。」'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}礼貌性问{target}：「介意我坐这儿吗？」其实早就坐下了。'},
    {'scope': 'same_table', 'relation': 'neutral', 'text': '{speaker}跟{target}点头致意，对话停在「今天天气不错」。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}把椅子往旁边挪了挪，尽量离{target}远一点。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}对{target}冷笑：「没想到你也来这种地方放松。」'},
    {'scope': 'same_table', 'relation': 'hostile', 'text': '{speaker}把茶杯重重放下：「{target}，别看我，看你自己的杯子。」'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}盯着{target}：「下次下本，各走各的，别拖后腿。」'},
    # ── 隔桌 ────────────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}对{target}说：「今天茶不错，比昨天那壶强。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你那杯什么茶？闻着像花茶。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}碰了碰茶杯，瓷器声清脆。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}提醒{target}：「你袖子沾到茶水了——现在没事了。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}笑道：「下次还来这桌，我请客……可能。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}望着{target}，轻声说：「看见你活着真好。」'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}瞥了眼{target}，把话咽回去，只多喝了一口茶。'},
    {'scope': 'same_table', 'relation': 'hostile', 'text': '{speaker}对{target}冷冷道：「别看我，喝你自己的。」'},
    # ── 多句 / 对白感 ─────────────────────────────────────────────────────
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}：「你信命运吗？」{target}：「信，但信它会给第二次机会。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}：「今天差点回不来。」{target}：「所以你在喝茶，我在听你活着。」'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你那队还缺人吗？」{target}：「缺，但缺的是靠谱的。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}自言自语：「再坐一刻钟，就回宿舍。」——一刻钟过去了，还在。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}对茶壶说：「别倒了，我就喝最后一杯。」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶叶渣倒过来看了看，像在占卜。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}跟{target}比赛谁先把杯沿上的水珠舔干净。'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}把毯子分给{target}一半：「夜里冷，别逞强。」'},
    {'scope': 'same_table', 'relation': 'close', 'text': '{speaker}什么也没说，只是把{target}的茶杯续满了。'},
    {'scope': 'same_table', 'relation': 'any', 'text': '{speaker}问{target}：「你那队人齐了吗？」答：「人齐了，命还没齐。」'},
    {'scope': 'same_table', 'relation': 'friendly', 'text': '{speaker}冲{target}喊：「下回别抢我人头！」'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}在桌上用茶渍画了个笑脸，又迅速擦掉。'},
    {'scope': 'solo', 'relation': 'any', 'text': '{speaker}把茶杯立起来，赌它能不能立到第二杯。'},
    {'scope': 'same_table', 'relation': 'strained', 'text': '{speaker}跟{target}同桌喝茶，却全程只跟茶壶说话。'},
    {'scope': 'same_table', 'relation': 'rival', 'text': '{speaker}对{target}举杯：「敬你活着——也敬我活得比你久。」'},
]


@dataclass
class _SeatCtx:
    seat: AdventurerS1TeaHouseSeat
    adv_id: int
    adv_name: str


class AdventurerS1TeaHouseChatterService:
    """茶舍环境闲聊生成器。"""

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
        seats: Sequence[AdventurerS1TeaHouseSeat],
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
        for tpl in S1_TEA_HOUSE_CHATTER_TEMPLATES:
            scope = tpl.get('scope', 'solo')
            if scope == 'solo':
                if _relation_matches(tpl.get('relation', 'any'), 'neutral'):
                    viable.append((tpl, None))
                continue

            candidates = AdventurerS1TeaHouseChatterService._candidates_for_scope(
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
        return AdventurerS1TeaHouseChatterService._format_line(tpl, speaker, target)

    @staticmethod
    def generate_for_seats(
        seats: Sequence[AdventurerS1TeaHouseSeat],
        adv_name_map: Dict[int, str],
        stale_seat_ids: Iterable[int],
        now: Optional[datetime] = None,
    ) -> Dict[int, Optional[str]]:
        """为每个有效占座座位生成闲聊文案。返回 seat_id -> chatter（无则 None）。"""
        prob = float(current_app.config.get('ADVENTURER_S1_TAVERN_CHATTER_PROB', 0.35))
        prob = min(max(prob, 0.0), 1.0)
        bucket = AdventurerS1TeaHouseChatterService._tick_bucket(now)

        occupied = AdventurerS1TeaHouseChatterService._build_context(
            seats, adv_name_map, stale_seat_ids,
        )
        if not occupied:
            return {}

        rel_map = AdventurerS1TeaHouseChatterService._load_relationship_map(
            [c.adv_id for c in occupied],
        )

        result: Dict[int, Optional[str]] = {}
        for ctx in occupied:
            rng = random.Random(f's1-tea-chatter:{ctx.seat.id}:{ctx.adv_id}:{bucket}')
            if rng.random() > prob:
                result[ctx.seat.id] = None
                continue
            line = AdventurerS1TeaHouseChatterService._pick_template_and_target(
                ctx, occupied, rel_map, rng,
            )
            result[ctx.seat.id] = line
        return result
