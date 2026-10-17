"""深渊食堂：菜单、用餐结算与下次探险一次性临时加成。"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1DiningMenuItem

BUFF_STAT_LABELS = {
    'strength': '力量',
    'agility': '敏捷',
    'intelligence': '智力',
    'luck': '运气',
    'max_hp': '生命上限',
    'exp_pct': '经验',
}

ALLOWED_BUFF_STATS = frozenset(BUFF_STAT_LABELS.keys())


class S1DiningService:
    """食堂菜单查询与探险 buff 应用/消耗。"""

    @staticmethod
    def normalize_buffs(source: Any) -> list[dict]:
        """统一 buff 列表：[{stat, amount}, ...]。"""
        if isinstance(source, dict) and isinstance(source.get('buffs'), list):
            raw = source['buffs']
        elif isinstance(source, AdventurerS1DiningMenuItem):
            if isinstance(source.buffs, list) and source.buffs:
                raw = source.buffs
            else:
                raw = [{'stat': source.buff_stat, 'amount': source.buff_amount}]
        elif isinstance(source, list):
            raw = source
        elif isinstance(source, dict) and source.get('buff_stat'):
            raw = [{'stat': source['buff_stat'], 'amount': source.get('buff_amount', 0)}]
        else:
            raw = []

        buffs: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            stat = str(item.get('stat') or '').strip()
            amount = int(item.get('amount') or 0)
            if stat in ALLOWED_BUFF_STATS and amount > 0:
                buffs.append({'stat': stat, 'amount': amount})
        return buffs

    @staticmethod
    def format_buffs_label(buffs: list[dict], *, scope: str = 'next') -> str:
        """生成加成文案。scope='next' 用于食堂菜单（用餐后下次出发生效），'current' 用于出发日志（本次探险）。"""
        parts = []
        for b in buffs:
            stat = b['stat']
            amount = int(b['amount'])
            label = BUFF_STAT_LABELS.get(stat, stat)
            suffix = '%' if stat == 'exp_pct' else ''
            parts.append(f'{label}+{amount}{suffix}')
        if not parts:
            return ''
        prefix = '本次探险' if scope == 'current' else '下次探险'
        return f"{prefix}{'、'.join(parts)}"

    @staticmethod
    def format_buff_label(buff_stat: str, buff_amount: int) -> str:
        return S1DiningService.format_buffs_label(
            [{'stat': buff_stat, 'amount': int(buff_amount or 0)}],
        )

    @staticmethod
    def list_menu_items() -> list[dict]:
        rows = (
            AdventurerS1DiningMenuItem.query
            .filter_by(is_active=True)
            .order_by(AdventurerS1DiningMenuItem.sort_order.asc(), AdventurerS1DiningMenuItem.id.asc())
            .all()
        )
        items = []
        for row in rows:
            buffs = S1DiningService.normalize_buffs(row)
            items.append({
                'code': row.code,
                'name': row.name,
                'icon': row.icon,
                'description': row.description,
                'gold_cost': int(row.gold_cost or 0),
                'buff_label': S1DiningService.format_buffs_label(buffs),
            })
        return items

    @staticmethod
    def get_menu_item_by_code(code: str) -> Optional[AdventurerS1DiningMenuItem]:
        code = str(code or '').strip()
        if not code:
            return None
        return AdventurerS1DiningMenuItem.query.filter_by(code=code, is_active=True).first()

    @staticmethod
    def resolve_food(food_code: str) -> Tuple[Optional[dict], Optional[str]]:
        item = S1DiningService.get_menu_item_by_code(food_code)
        if not item:
            return None, '请选择有效的餐品'
        buffs = S1DiningService.normalize_buffs(item)
        if not buffs:
            return None, '餐品配置无效'
        return {
            'code': item.code,
            'name': item.name,
            'icon': item.icon,
            'gold_cost': int(item.gold_cost or 0),
            'buffs': buffs,
            'buff_label': S1DiningService.format_buffs_label(buffs),
        }, None

    @staticmethod
    def build_expedition_buff_payload(food: dict) -> dict:
        buffs = S1DiningService.normalize_buffs(food)
        return {
            'food_code': food['code'],
            'food_name': food['name'],
            'buffs': buffs,
            'label': food.get('buff_label') or S1DiningService.format_buffs_label(buffs),
        }

    @staticmethod
    def apply_buff_to_adventurer(adv: AdventurerS1, food: dict) -> None:
        adv.expedition_buff = S1DiningService.build_expedition_buff_payload(food)

    @staticmethod
    def consume_expedition_buff(adv: AdventurerS1) -> Optional[dict]:
        buff = adv.expedition_buff
        adv.expedition_buff = None
        return buff if isinstance(buff, dict) else None

    @staticmethod
    def consume_buffs_for_members(members: list) -> list[dict]:
        consumed = []
        for member in members or []:
            buff = S1DiningService.consume_expedition_buff(member)
            if buff:
                consumed.append({'adventurer_id': int(member.id), 'buff': buff})
        return consumed

    @staticmethod
    def apply_buffs_to_snapshot_values(
        base_strength: int,
        base_agility: int,
        base_intelligence: int,
        base_luck: int,
        base_hp: int,
        base_max_hp: int,
        buff: Optional[dict],
    ) -> tuple[int, int, int, int, int, int]:
        """把用餐加成叠到探险快照（仅快照，不改 ORM）。

        max_hp 加成只抬高探险内上限，不额外增加当前生命值。
        """
        strength = base_strength
        agility = base_agility
        intelligence = base_intelligence
        luck = base_luck
        hp = base_hp
        max_hp = base_max_hp

        for b in S1DiningService.normalize_buffs(buff or {}):
            stat = b['stat']
            amount = int(b['amount'])
            if stat == 'strength':
                strength += amount
            elif stat == 'agility':
                agility += amount
            elif stat == 'intelligence':
                intelligence += amount
            elif stat == 'luck':
                luck += amount
            elif stat == 'max_hp':
                max_hp += amount
                hp = min(hp, max_hp)

        return strength, agility, intelligence, luck, hp, max_hp

    @staticmethod
    def build_dining_context_entry(
        member_id: int,
        departure_hp: int,
        departure_max_hp: int,
        snapshot_hp: int,
        snapshot_max_hp: int,
        buff: Optional[dict] = None,
    ) -> dict:
        entry = {
            'departure_hp': int(departure_hp),
            'departure_max_hp': int(departure_max_hp),
            'snapshot_hp': int(snapshot_hp),
            'snapshot_max_hp': int(snapshot_max_hp),
        }
        exp_bonus_pct = S1DiningService.get_expedition_exp_bonus_pct(buff)
        if exp_bonus_pct > 0:
            entry['exp_bonus_pct'] = exp_bonus_pct
        return entry

    @staticmethod
    def get_expedition_exp_bonus_pct(source: Optional[dict]) -> int:
        """读取用餐配置里的探险经验百分比加成。"""
        if not isinstance(source, dict):
            return 0
        if source.get('exp_bonus_pct') is not None:
            return max(0, int(source.get('exp_bonus_pct') or 0))
        for b in S1DiningService.normalize_buffs(source):
            if b['stat'] == 'exp_pct':
                return int(b['amount'])
        return 0

    @staticmethod
    def resolve_post_expedition_hp(
        remaining_hp: int,
        dining_context: Optional[dict],
        fallback_max_hp: int,
    ) -> int:
        """把 AI 基于快照上限返回的 remaining_hp 映射回真实 max_hp 尺度。

        用「相对快照起始 HP 的变化量」换算，避免临时 max_hp 加成被当成永久上限，
        也避免归来时错误扣减当前生命值。
        """
        remaining = max(0, int(remaining_hp))
        dep_max = max(1, int(fallback_max_hp or 100))

        if not isinstance(dining_context, dict):
            return min(dep_max, remaining)

        dep_hp = max(0, int(dining_context.get('departure_hp', 0)))
        snap_hp = max(0, int(dining_context.get('snapshot_hp', dep_hp)))
        snap_max = max(snap_hp, int(dining_context.get('snapshot_max_hp', dep_max)))
        dep_max_ctx = max(1, int(dining_context.get('departure_max_hp', dep_max)))

        # AI 返回的是探险内剩余 HP；按与快照起点的差值映射到真实 HP
        delta = remaining - snap_hp
        resolved = dep_hp + delta
        resolved = max(0, min(dep_max_ctx, resolved))
        # 若 AI 误用快照上限做绝对值（remaining 接近 snap_max），仍不超过真实 max
        if remaining > dep_max_ctx and delta > 0:
            resolved = min(dep_max_ctx, resolved)
        return resolved

    @staticmethod
    def build_departure_buff_events(members: list) -> list[str]:
        events = []
        for member in members or []:
            buff = getattr(member, 'expedition_buff', None)
            if not isinstance(buff, dict):
                continue
            food_name = buff.get('food_name') or '餐点'
            label = S1DiningService.format_buffs_label(
                S1DiningService.normalize_buffs(buff),
                scope='current',
            )
            events.append(f'{member.name} 带着「{food_name}」的加成出发了（{label}）。')
        return events
