"""管理员回收数据申诉历史漏洞收益。"""
from runtime_core.time import utc_now as _portable_utc_now
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from flask import current_app
from sqlalchemy import and_, func, or_, text

from db_repository import db_manager
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BugReclaimBatch,
    AdventurerS1BugReclaimCandidate,
    AdventurerS1BugReclaimItem,
    AdventurerS1BugReclaimScanState,
    AdventurerS1CurrencyLog,
    AdventurerS1DataAppeal,
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1ItemLog,
    AdventurerS1MarketListing,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_buyback_service import S1BuybackService
from plugins.adventurer_s1.services.s1_bug_appeal_qty_guard import S1BugAppealQtyGuard
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService
from plugins.adventurer_s1.services.s1_item_instance_service import consume_item_stack
from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of
from utils.time_utils import to_utc_iso


@dataclass(frozen=True)
class _ReclaimAuditSnapshot:
    id: int
    adventurer_id: int
    op_type: str
    target_type: str
    target_id: Optional[int]
    before_json: Optional[dict]
    after_json: Optional[dict]
    created_at: Optional[datetime]
    note: Optional[str] = None


@dataclass(frozen=True)
class _ReclaimAppealSnapshot:
    id: int
    user_id: int
    expedition_log_id: Optional[int]
    created_at: Optional[datetime]
    applied_at: Optional[datetime]
    snapshot_before: Optional[dict]
    player_note: Optional[str] = None
    ai_summary: Optional[str] = None


_QTY_UNITS = '个|件|枚|瓶|支|份|颗|张|本|块|袋|株|把|根|条|粒|片|单位'
_CN_DIGITS = r'[零〇一二两三四五六七八九十百千万]+'
_QTY_NUM = rf'(?:\d+|{_CN_DIGITS})'
_STACK_SUFFIX_RE = re.compile(
    rf'\s*(?:[xX×＊*]\s*{_QTY_NUM}|[（(]\s*{_QTY_NUM}[^）)]*[）)]|{_QTY_NUM}\s*(?:{_QTY_UNITS})?)\s*$'
)
_STACK_PREFIX_RE = re.compile(
    rf'^(?:[xX×＊*]\s*{_QTY_NUM}\s*|[（(]\s*{_QTY_NUM}[^）)]*[）)]\s*|{_QTY_NUM}\s*(?:{_QTY_UNITS})?\s*)+'
)
_GAIN_CHANGE_TYPES = ('gain', 'overflow')
_DETECTION_MODE = 'appeal_qty_v10_chunked_target_scoped_seed'
_INCREMENTAL_SCAN_ADVISORY_LOCK_KEY = 910630161
_DEFAULT_MISSING_ITEM_PRICE_FLOORS = {
    'common': 100, 'rare': 500, 'epic': 1200, 'legendary': 2500,
}
_SOURCE_TYPE_LABELS = {
    'expedition': '探险/副本掉落',
    'expedition_reward': '探险结算奖励',
    'expedition_loss': '探险结算扣减',
    'special_dungeon_entry': '特殊副本',
    'shop': '商店购买',
    'shop_buy': '商店购买',
    'market': '交易市场',
    'market_buy': '市场购入',
    'market_sell': '市场出售',
    'data_appeal_ai': '数据申诉补发',
    'data_appeal_reclaim': '申诉追回',
    'data_appeal_reclaim_comp': '申诉追回补偿',
    'data_appeal_revert': '申诉还原',
    'grant': '系统发放',
    'building': '建筑系统',
    'blacksmith': '铁匠铺',
    'tailor_workshop': '绮罗裁坊',
    'alchemy_workshop': '炼金工房',
    'lucky_wheel': '幸运大转盘',
    'secret_auction': '隐秘拍卖行',
    'clue_bureau': '线索局',
    'hidden_boss_clue_redeem': '隐藏 Boss 兑换',
    'buyback_sell': '收购商行',
    'insurance_payout': '保险赔付',
    'admin': '管理员操作',
    'admin_adjust': '管理员调整',
    'transfer': '玩家转移',
    'tavern_drink': '酒馆点酒',
    'tavern_food': '酒馆点餐',
    'tavern_treat': '酒馆请客',
    'tea_house_entry': '深渊茶舍',
    'hot_spring_entry': '温泉',
    'commission': '悬赏委托',
    'treasure_map': '藏宝图',
    'unknown': '未知来源',
}


class S1BugReclaimService:
    """预览并执行由历史申诉漏洞造成的异常收益回收。"""

    @staticmethod
    def _text(value) -> str:
        return str(value or '').strip()

    @staticmethod
    def _missing_item_unit_price(unit_recycle_price: float, rarity: str) -> float:
        """实物无法追回时使用稀有度最低缺货价，且不会低于正常回收价。"""
        floors = current_app.config.get('ADVENTURER_S1_BUG_RECLAIM_MISSING_ITEM_PRICE_FLOORS')
        if not isinstance(floors, dict):
            floors = _DEFAULT_MISSING_ITEM_PRICE_FLOORS
        rarity_key = rarity if rarity in _DEFAULT_MISSING_ITEM_PRICE_FLOORS else 'common'
        try:
            configured_floor = float(floors.get(rarity_key, _DEFAULT_MISSING_ITEM_PRICE_FLOORS[rarity_key]))
        except (TypeError, ValueError):
            configured_floor = float(_DEFAULT_MISSING_ITEM_PRICE_FLOORS[rarity_key])
        return round(max(1.0, float(unit_recycle_price), configured_floor), 2)

    @staticmethod
    def _target_slot(audit, before: dict, after: dict) -> tuple[int, str, int] | None:
        if audit.target_type not in ('equipment', 'warehouse'):
            return None
        target_id = audit.target_id or before.get('id') or after.get('id')
        try:
            return (int(audit.adventurer_id), str(audit.target_type), int(target_id))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_identity_fork(before: dict, after: dict) -> bool:
        """同条 audit 同时写入 name + custom_desc，且至少一项与 before 不同。"""
        if 'name' not in after or 'custom_desc' not in after:
            return False
        nb = S1BugReclaimService._text(before.get('name'))
        na = S1BugReclaimService._text(after.get('name') or before.get('name'))
        db = S1BugReclaimService._text(before.get('custom_desc'))
        da = S1BugReclaimService._text(after.get('custom_desc'))
        return na != nb or da != db

    @staticmethod
    def _appeal_deleted_slots(rows) -> dict[int, set[tuple[str, int]]]:
        deleted: dict[int, set[tuple[str, int]]] = {}
        for audit, appeal in rows:
            if audit.op_type != 'delete' or audit.target_type not in ('equipment', 'warehouse'):
                continue
            try:
                target_id = int(audit.target_id)
            except (TypeError, ValueError):
                continue
            deleted.setdefault(int(appeal.id), set()).add((str(audit.target_type), target_id))
        return deleted

    @staticmethod
    def _snapshot_siblings(snapshot: dict, normalized: str, deleted_slots: set[tuple[str, int]]) -> list[dict]:
        hits: list[dict] = []
        for section in ('equipment', 'warehouse'):
            for rec in snapshot.get(section) or []:
                if not isinstance(rec, dict):
                    continue
                try:
                    slot = (section, int(rec.get('id')))
                except (TypeError, ValueError):
                    continue
                if slot in deleted_slots:
                    continue
                if S1BugReclaimService._normalize_name(rec.get('name')) != normalized:
                    continue
                hits.append({**rec, 'section': section})
        return hits

    @staticmethod
    def _appeal_source_key(appeal: AdventurerS1DataAppeal | _ReclaimAppealSnapshot) -> str | None:
        """Return the structured incident that an appeal is correcting, when available."""
        if appeal.expedition_log_id is not None:
            return f'expedition:{int(appeal.expedition_log_id)}'
        return None

    @staticmethod
    def _lineage_group_key(
        adventurer_id: int,
        normalized: str,
        item_type: str,
        rarity: str,
        appeal_source_key: str | None = None,
    ) -> str:
        base = f'dup:{adventurer_id}:{normalized}:{item_type or ""}:{rarity or "common"}'
        return f'{base}:source:{appeal_source_key}' if appeal_source_key else base

    @staticmethod
    def _normalize_name(value) -> str:
        text = str(value or '').strip()
        while True:
            next_text = _STACK_SUFFIX_RE.sub('', text).strip()
            if next_text == text:
                return text
            text = next_text

    @staticmethod
    def _catalog_fuzzy_name(value) -> str:
        """去掉数量前后缀后比较，如「三枚abc」与「abc」视为同名。"""
        text = S1BugReclaimService._normalize_name(value)
        while True:
            next_text = _STACK_PREFIX_RE.sub('', text).strip()
            if next_text == text:
                return text
            text = next_text

    @staticmethod
    def _record_cluster_current(cluster: dict, before: dict, after: dict) -> None:
        """记录申诉后最新名称/描述（物品已消耗时供预览回显）。"""
        if after.get('name'):
            cluster['current_name'] = after.get('name')
        elif before.get('name'):
            cluster.setdefault('current_name', before.get('name'))
        if 'custom_desc' in after:
            after_desc = after.get('custom_desc')
            if after_desc:
                cluster['current_desc'] = after_desc
                cluster['desc_cleaned'] = False
            else:
                cluster['current_desc'] = None
                cluster['desc_cleaned'] = True
        elif before.get('custom_desc') and not cluster.get('desc_cleaned'):
            cluster.setdefault('current_desc', before.get('custom_desc'))

    @staticmethod
    def _record_cluster_origin(cluster: dict, before: dict) -> None:
        """记录簇内最早见到的原名与申诉前 quantity 字段值。"""
        if not before or 'original_name' in cluster:
            return
        cluster['original_name'] = before.get('name')
        cluster['original_desc'] = before.get('custom_desc')
        cluster['origin_field_quantity'] = S1BugReclaimService._qty(before.get('quantity'))

    @staticmethod
    def _qty(value) -> int:
        try:
            return max(1, int(value or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _excluded_issue_keys() -> set[str]:
        """已执行回收或已标记误报的 issue_key，预览时跳过。"""
        rows = (
            db_manager.query(AdventurerS1BugReclaimItem.issue_key)
            .filter(AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')))
            .all()
        )
        return {str(r[0]) for r in rows if r and r[0]}

    @staticmethod
    def _issue_adjustments() -> dict[str, dict]:
        """管理员对待处理项的认定（非法数量 / 文字修正）。"""
        rows = (
            db_manager.query(AdventurerS1BugReclaimItem)
            .filter(AdventurerS1BugReclaimItem.status == 'adjusted')
            .all()
        )
        out: dict[str, dict] = {}
        for row in rows:
            meta = row.result_json or {}
            action = str(meta.get('action') or 'adjust_illegal_quantity')
            key = str(row.issue_key)
            if action == 'adjust_item_text':
                out[key] = {
                    'mode': 'text_correction',
                    'corrected_name': str(meta.get('corrected_name') or ''),
                    'corrected_desc': meta.get('corrected_desc'),
                    'corrected_quantity': meta.get('corrected_quantity'),
                    'note': str(meta.get('note') or ''),
                }
            else:
                out[key] = {
                    'mode': 'quantity',
                    'illegal_quantity': int(row.illegal_quantity or 0),
                    'computed_illegal_quantity': int(meta.get('computed_illegal_quantity') or 0),
                    'note': str(meta.get('note') or ''),
                }
        return out

    @staticmethod
    def _quantity_adjustments() -> dict[str, dict]:
        """兼容旧调用：仅非法数量认定。"""
        return {
            k: v for k, v in S1BugReclaimService._issue_adjustments().items()
            if v.get('mode') == 'quantity'
        }

    @staticmethod
    def _plan_text_correction(candidate: dict, adj: dict) -> dict:
        corrected_name = str(adj.get('corrected_name') or '').strip()
        corrected_desc = adj.get('corrected_desc')
        corrected_qty = adj.get('corrected_quantity')
        qty_note = f'，数量→{corrected_qty}' if corrected_qty is not None else ''
        details = {
            'corrected_name': corrected_name,
            'corrected_desc': corrected_desc,
            'corrected_quantity': corrected_qty,
            'target_type': candidate.get('target_type'),
            'target_id': candidate.get('target_id'),
        }
        return {
            'reclaim_mode': 'text_correction',
            'text_correction': {
                'corrected_name': corrected_name,
                'corrected_desc': corrected_desc,
                'corrected_quantity': corrected_qty,
                'note': adj.get('note') or '',
            },
            'reclaim_item_quantity': 0,
            'gold_to_deduct': 0.0,
            'buyer_refund_gold': 0.0,
            'seller_clawback_gold': 0.0,
            'route': [{
                'kind': 'text_correction',
                'confidence': 'exact',
                'quantity': 0,
                'gold': 0,
                'details': details,
                'message': f'修正为「{corrected_name}」{qty_note}，不扣物扣金。',
            }],
        }

    @staticmethod
    def _resolve_effective_illegal_quantity(
        candidate: dict,
        adjustments: dict[str, dict],
    ) -> tuple[dict, dict]:
        """返回用于 trace 的候选副本，以及预览项上的认定字段。"""
        computed = int(candidate.get('illegal_quantity') or 0)
        adj = adjustments.get(str(candidate.get('issue_key') or ''))
        extra: dict = {'computed_illegal_quantity': computed}
        trace_candidate = candidate
        if adj and adj.get('mode') == 'quantity':
            effective = int(adj.get('illegal_quantity') or 0)
            trace_candidate = {**candidate, 'illegal_quantity': effective}
            extra.update({
                'illegal_quantity': effective,
                'quantity_adjusted': True,
                'adjustment_note': adj.get('note') or '',
            })
        return trace_candidate, extra

    @staticmethod
    def _enrich_preview_item(
        candidate: dict,
        adjustments: dict[str, dict],
        adv_map: dict,
        user_map: dict,
    ) -> dict:
        analysis = S1BugReclaimService._quantity_analysis(candidate)
        adj = adjustments.get(str(candidate.get('issue_key') or ''))
        adv = adv_map.get(int(candidate['adventurer_id']))
        user = user_map.get(int(candidate['user_id'])) if candidate.get('user_id') else None
        base = {
            **{k: v for k, v in candidate.items() if not k.startswith('_')},
            'computed_illegal_quantity': int(candidate.get('illegal_quantity') or 0),
            'adventurer_name': adv.name if adv else '未知',
            'username': (user or {}).get('username'),
            'nickname': (user or {}).get('nickname'),
            'quantity_analysis': analysis,
        }
        if candidate.get('_since'):
            from utils.time_utils import to_utc_iso
            base['trace_since'] = to_utc_iso(candidate['_since'])

        if adj and adj.get('mode') == 'text_correction':
            return {**base, **S1BugReclaimService._plan_text_correction(candidate, adj)}

        trace_candidate, qty_fields = S1BugReclaimService._resolve_effective_illegal_quantity(
            candidate, adjustments,
        )
        trace = S1BugReclaimService._trace_route(trace_candidate)
        return {**base, **qty_fields, **trace}

    @staticmethod
    def _executed_issue_keys() -> set[str]:
        return S1BugReclaimService._excluded_issue_keys()

    @staticmethod
    def _user_map(user_ids: set[int]) -> dict[int, dict]:
        if not user_ids:
            return {}
        rows = db_manager.query(User).filter(User.id.in_(user_ids)).all()
        return {
            int(u.id): {
                'user_id': u.id,
                'username': u.username,
                'nickname': u.nickname,
            }
            for u in rows
        }

    @staticmethod
    def _adventurer_map(adventurer_ids: set[int]) -> dict[int, AdventurerS1]:
        if not adventurer_ids:
            return {}
        rows = db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(adventurer_ids)).all()
        return {int(a.id): a for a in rows}

    @staticmethod
    def _resolve_update_cluster_key(
        audit: AdventurerS1DataAppealAuditLog | _ReclaimAuditSnapshot,
        before: dict,
        after: dict,
    ) -> tuple[str | None, str, str, str, int | None, str | None]:
        after_name = after.get('name', before.get('name'))
        normalized = S1BugReclaimService._normalize_name(after_name or before.get('name'))
        item_type = str(after.get('type', before.get('type') or ''))
        rarity = str(after.get('rarity', before.get('rarity') or 'common'))
        target_id = audit.target_id or before.get('id') or after.get('id')
        if target_id is not None:
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                target_id = None
        if target_id is not None:
            key = S1BugReclaimService._cluster_key_target(
                int(audit.adventurer_id), audit.target_type, target_id,
            )
            return key, normalized, item_type, rarity, target_id, audit.target_type
        key = S1BugReclaimService._cluster_key_name(
            int(audit.adventurer_id), normalized, item_type, rarity,
        )
        return key, normalized, item_type, rarity, None, None

    @staticmethod
    def _source_label(source_type: str | None) -> str:
        key = str(source_type or '').strip() or 'unknown'
        return _SOURCE_TYPE_LABELS.get(key, key or '未知来源')

    @staticmethod
    def _truncate_text(value, max_len: int = 160) -> str | None:
        text = str(value or '').strip()
        if not text:
            return None
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + '…'

    @staticmethod
    def _is_passthrough_gain_log(log, adventurer_id: int) -> bool:
        """中转型 gain 流水不算真实来源（需继续往前回溯）。"""
        desc = str(log.description or '')
        if '取消挂单退回' in desc:
            return True
        if log.source_type == 'market' and log.related_id and str(log.related_id).isdigit():
            listing = (
                db_manager.query(AdventurerS1MarketListing)
                .filter_by(id=int(log.related_id))
                .first()
            )
            if listing and int(listing.seller_adventurer_id) == int(adventurer_id):
                # 自己挂单下架退回；成交换入等「换入/购入」才是真实来源
                if '交易市场购入' not in desc and '换入' not in desc:
                    return True
        return False

    @staticmethod
    def _resolve_effective_gain_log(
        adventurer_id: int,
        item_name: str,
        aliases: set[str],
        stack_created_at=None,
    ) -> tuple:
        """从最新 gain 往前跳过中转流水，返回 (有效来源 log, 已跳过的 log 列表)。"""
        name_set = {str(item_name).strip()} | {a for a in aliases if a}
        logs = (
            db_manager.query(AdventurerS1ItemLog)
            .filter(
                AdventurerS1ItemLog.adventurer_id == adventurer_id,
                AdventurerS1ItemLog.item_name.in_(name_set),
                AdventurerS1ItemLog.change_type.in_(_GAIN_CHANGE_TYPES),
            )
            .order_by(AdventurerS1ItemLog.created_at.asc(), AdventurerS1ItemLog.id.asc())
            .limit(300)
            .all()
        )
        if not logs:
            return None, []
        if stack_created_at is not None:
            # item_log 往往比 warehouse 行 insert 晚几毫秒，需留同窗容差
            from datetime import timedelta
            cutoff = stack_created_at + timedelta(seconds=5)
            eligible = [
                log for log in logs
                if log.created_at and log.created_at <= cutoff
            ]
        else:
            eligible = list(logs)
        skipped: list = []
        while eligible:
            candidate = eligible[-1]
            if S1BugReclaimService._is_passthrough_gain_log(candidate, adventurer_id):
                skipped.append(candidate)
                eligible.pop()
                continue
            return candidate, skipped
        return None, skipped

    @staticmethod
    def _format_gain_log_brief(log) -> dict:
        return {
            'item_log_id': log.id,
            'description': str(log.description or ''),
            'source_type': log.source_type,
            'quantity': int(log.quantity or 1),
            'at': to_utc_iso(log.created_at) if log.created_at else None,
        }

    @staticmethod
    def _find_stack_gain_log(
        adventurer_id: int,
        item_name: str,
        aliases: set[str],
        stack_created_at=None,
    ):
        gain_log, _skipped = S1BugReclaimService._resolve_effective_gain_log(
            adventurer_id, item_name, aliases, stack_created_at,
        )
        return gain_log

    @staticmethod
    def _pick_closest_currency_log(rows, around_dt):
        if not rows:
            return None
        if not around_dt or len(rows) == 1:
            return rows[0]

        def _distance(row) -> float:
            if not row.created_at:
                return 1e18
            return abs((row.created_at - around_dt).total_seconds())

        return min(rows, key=_distance)

    @staticmethod
    def _market_listing_currency_amount(
        adventurer_id: int,
        source_type: str,
        listing_id: int,
        around_dt=None,
        *,
        positive_only: bool = False,
        negative_only: bool = False,
    ) -> float:
        rows = (
            db_manager.query(AdventurerS1CurrencyLog)
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == int(adventurer_id),
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == source_type,
                AdventurerS1CurrencyLog.related_id == str(listing_id),
                AdventurerS1CurrencyLog.related_type == 'market_listing',
            )
            .order_by(AdventurerS1CurrencyLog.created_at.asc())
            .all()
        )
        if negative_only:
            rows = [r for r in rows if float(r.change_amount or 0) < -0.001]
        elif positive_only:
            rows = [r for r in rows if float(r.change_amount or 0) > 0.001]
        row = S1BugReclaimService._pick_closest_currency_log(rows, around_dt)
        if not row:
            return 0.0
        amount = abs(float(row.change_amount or 0))
        if source_type == 'market_sell' and positive_only:
            amount += S1BugReclaimService._market_sell_temporary_income(
                adventurer_id,
                listing_id,
                around_dt=row.created_at,
                closest_only=True,
            )
        return round(amount, 2)

    @staticmethod
    def _market_sell_temporary_income(
        adventurer_id: int,
        listing_id: int,
        around_dt=None,
        *,
        closest_only: bool = False,
    ) -> float:
        """读取同笔市场售出分到临时金的金额，兼容旧版额外累积流水。"""
        rows = (
            db_manager.query(AdventurerS1CurrencyLog)
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == int(adventurer_id),
                AdventurerS1CurrencyLog.currency_code
                == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                AdventurerS1CurrencyLog.source_type.in_([
                    'low_consumption_accrual',
                    'low_consumption_split',
                ]),
                AdventurerS1CurrencyLog.related_id == str(listing_id),
                AdventurerS1CurrencyLog.related_type == 'market_listing',
                AdventurerS1CurrencyLog.change_amount > 0,
            )
            .order_by(AdventurerS1CurrencyLog.created_at.asc())
            .all()
        )
        if closest_only:
            row = S1BugReclaimService._pick_closest_currency_log(rows, around_dt)
            return round(float(row.change_amount or 0), 2) if row else 0.0
        return round(sum(float(row.change_amount or 0) for row in rows), 2)

    @staticmethod
    def _resolve_market_purchase_provenance(candidate: dict) -> dict | None:
        """目标堆若来自市场卖钱购入，解析卖家与成交金币（供撤销方案使用）。"""
        items = S1BugReclaimService._current_items(candidate)
        if not items:
            return None
        source, row = items[0]
        adventurer_id = int(candidate['adventurer_id'])
        aliases = S1BugReclaimService._aliases(candidate)
        gain_log, skipped_logs = S1BugReclaimService._resolve_effective_gain_log(
            adventurer_id,
            row.name,
            aliases,
            getattr(row, 'created_at', None),
        )
        if not gain_log or not gain_log.related_id or not str(gain_log.related_id).isdigit():
            return None
        if '交易市场购入' not in str(gain_log.description or ''):
            return None

        listing_id = int(gain_log.related_id)
        listing = (
            db_manager.query(AdventurerS1MarketListing)
            .filter_by(id=listing_id)
            .first()
        )
        if not listing or listing.listing_type != 'sell':
            return None

        seller_id = int(listing.seller_adventurer_id)
        if seller_id == adventurer_id:
            return None

        around = gain_log.created_at
        buyer_paid = S1BugReclaimService._market_listing_currency_amount(
            adventurer_id, 'market_buy', listing_id, around, negative_only=True,
        )
        seller_income = S1BugReclaimService._market_listing_currency_amount(
            seller_id, 'market_sell', listing_id, around, positive_only=True,
        )
        if buyer_paid <= 0 and listing.price:
            purchase_qty = max(1, int(gain_log.quantity or 1))
            listing_qty = max(1, int(listing.quantity or 1))
            buyer_paid = round(float(listing.price) * (purchase_qty / listing_qty), 2)
        if seller_income <= 0:
            seller_income = S1BugReclaimService._market_sell_income(seller_id, listing_id)

        seller = db_manager.query(AdventurerS1).filter_by(id=seller_id).first()
        seller_user = None
        if seller and seller.owner_id:
            seller_user = db_manager.query(User).filter_by(id=int(seller.owner_id)).first()

        purchase_qty = max(1, int(gain_log.quantity or 1))
        stack_qty = item_quantity_of(row)
        seller_name = seller.name if seller else None

        return {
            'listing_id': listing_id,
            'listing_type': listing.listing_type,
            'listing_status': listing.status,
            'gain_log_id': gain_log.id,
            'purchase_quantity': purchase_qty,
            'stack_quantity': stack_qty,
            'stack_source': source,
            'stack_id': int(row.id),
            'buyer_adventurer_id': adventurer_id,
            'seller_adventurer_id': seller_id,
            'seller_name': seller_name,
            'seller_user_id': int(seller.owner_id) if seller and seller.owner_id else None,
            'seller_username': seller_user.username if seller_user else None,
            'seller_nickname': seller_user.nickname if seller_user else None,
            'buyer_paid': buyer_paid,
            'seller_income': seller_income,
            'purchased_at': to_utc_iso(around) if around else None,
            'item_name_at_purchase': listing.item_name,
            'source_trace': {
                'skipped': [S1BugReclaimService._format_gain_log_brief(s) for s in skipped_logs],
                'effective': S1BugReclaimService._format_gain_log_brief(gain_log),
            } if skipped_logs else None,
            'summary_text': (
                f'市场购入：卖家 {seller_name or "?"} (#{seller_id})'
                f' · 挂单 #{listing_id} · 本次购入 ×{purchase_qty}'
                f' · 实付 {buyer_paid} 金 · 卖家到账 {seller_income} 金'
                + (f'（已跳过 {len(skipped_logs)} 条中转流水）' if skipped_logs else '')
            ),
        }

    @staticmethod
    def _human_step_summary(step: dict) -> str:
        op = str(step.get('op_type') or '')
        if op == 'create_item':
            name = step.get('name_after') or '未命名物品'
            qty = step.get('qty_after')
            qty_s = int(qty) if qty is not None else 1
            return f'新增物品「{name}」×{qty_s}（新建堆，不是改已有堆的 quantity）'
        if op == 'update':
            tt = step.get('target_type') or '记录'
            tid = step.get('target_id')
            target = f'{tt}#{tid}' if tid is not None else str(tt)
            chunks: list[str] = []
            nb, na = step.get('name_before'), step.get('name_after')
            if nb is not None and na is not None and str(nb) != str(na):
                chunks.append(f'名称 {nb}→{na}')
            qb, qa = step.get('qty_before'), step.get('qty_after')
            if qb is not None and qa is not None and qb != qa:
                chunks.append(f'数量 {qb}→{qa}（改已有堆 quantity，非新建）')
            db, da = step.get('desc_before'), step.get('desc_after')
            if (db or da) and db != da:
                chunks.append('描述变更')
            if chunks:
                return f'修改 {target}：{"；".join(chunks)}'
            return f'修改 {target}'
        if op == 'delete':
            tt = step.get('target_type') or '记录'
            tid = step.get('target_id')
            target = f'{tt}#{tid}' if tid is not None else str(tt)
            return f'删除 {target}'
        return op or '未知操作'

    @staticmethod
    def _issue_verdict_text(cluster: dict, seed_appeal_id: int | None, fork_appeal_id: int | None) -> str:
        issue = cluster.get('issue_type') or ''
        if issue == 'duplicate_lineage_reappeal':
            seed = seed_appeal_id or '?'
            return (
                f'申诉 #{seed} 起，快照中已有同名物品时又执行了补发（create_item）；'
                f'此后同 lineage 上的改量或再次补发均计为非法增量。'
            )
        if issue == 'identity_reappeal':
            fork = fork_appeal_id or '?'
            return (
                f'这是两次（或以上）独立的玩家申诉：先 #{fork} 归位名称/描述，'
                f'后一次又改 quantity。规则上后者计非法；若后者只是补全前次漏改的数量，可标记误报。'
            )
        if issue == 'appeal_create_grant':
            return (
                '申诉 audit 含 create_item（新建物品堆）；同链路内再次 create_item 或后续改 quantity '
                '均计为非法增量，进入回收预览。'
            )
        if issue == 'appeal_qty_increase':
            return (
                '已通过申诉 audit 增加 quantity；若同物品链路中已有 create_item，改量一并计非法；'
                '纯改量且无 create_item 时，首次数量归一仍视为合法种子。'
            )
        return '同物申诉链上存在非法增量，详见逐步明细。'

    @staticmethod
    def _appeal_group_player_summary(group: dict) -> str:
        parts: list[str] = []
        for step in group.get('steps') or []:
            op = step.get('op_type')
            if op == 'create_item':
                parts.append(f'补发新物品「{step.get("name_after") or "物品"}」×{step.get("qty_after") or 1}')
                continue
            if op != 'update':
                continue
            chunks: list[str] = []
            nb, na = step.get('name_before'), step.get('name_after')
            if nb is not None and na is not None and str(nb) != str(na):
                chunks.append(f'名称 {nb}→{na}')
            qb, qa = step.get('qty_before'), step.get('qty_after')
            if qb is not None and qa is not None and qb != qa:
                chunks.append(f'quantity {qb}→{qa}')
            elif qb is not None and (qa is None or qa == qb):
                chunks.append(f'quantity 未改（仍为 {int(qb)}）')
            db, da = step.get('desc_before'), step.get('desc_after')
            if (db or da) and db != da:
                chunks.append('描述变更')
            if chunks:
                tt = step.get('target_type') or ''
                tid = step.get('target_id')
                target = f'{tt}#{tid}' if tid is not None else tt
                parts.append(f'修改 {target}：{"；".join(chunks)}')
        if not parts:
            return '玩家此次申诉（见下方字段明细）'
        return '玩家此次申诉：' + '；'.join(parts)

    @staticmethod
    def _appeal_group_system_verdict(
        group: dict,
        *,
        issue_type: str,
        fork_appeal_id: int | None,
        seed_appeal_id: int | None,
    ) -> str:
        aid = int(group['appeal_id'])
        if not group.get('is_illegal'):
            if group.get('is_fork'):
                return (
                    '系统判定：本次申诉同条 audit 写入 name + custom_desc → 身份归位，'
                    '不计非法。'
                )
            if group.get('is_seed'):
                return '系统判定：快照中已有同名物品时的补发 → 记为 lineage 种子，不计非法。'
            return '系统判定：本次不计非法。'
        illegal = int(group.get('illegal_quantity') or 0)
        if issue_type == 'identity_reappeal' and fork_appeal_id:
            return (
                f'系统判定：#{fork_appeal_id} 已归位后，#{aid} 又增加 quantity → 记非法 +{illegal}。'
                f'若 #{aid} 只是在补全 #{fork_appeal_id} 当时未改的 quantity（玩家拆成两次申诉），'
                f'可点「标记误报」。'
            )
        if issue_type == 'appeal_qty_increase':
            return (
                f'系统判定：#{aid} 通过申诉将 quantity 增加 +{illegal}。'
                f'若属首次纠错或合理改量，可点「标记误报」。'
            )
        if issue_type == 'duplicate_lineage_reappeal' and seed_appeal_id:
            return (
                f'系统判定：#{seed_appeal_id} 重复 lineage 建立后，#{aid} 又改量/补发 → 记非法 +{illegal}。'
            )
        return f'系统判定：本次记非法 +{illegal}。'

    @staticmethod
    def _build_appeal_groups(
        steps: list[dict],
        *,
        fork_appeal_id: int | None,
        seed_appeal_id: int | None,
        issue_type: str,
    ) -> list[dict]:
        by_id: dict[int, dict] = {}
        for step in steps:
            aid = int(step['appeal_id'])
            group = by_id.setdefault(aid, {
                'appeal_id': aid,
                'steps': [],
                'is_illegal': False,
                'illegal_quantity': 0,
                'is_fork': False,
                'is_seed': False,
            })
            group['steps'].append(step)
            if step.get('is_illegal'):
                group['is_illegal'] = True
                group['illegal_quantity'] += int(step.get('illegal_quantity') or 0)
            if step.get('kind') == 'seed_fork' or (fork_appeal_id and aid == int(fork_appeal_id)):
                group['is_fork'] = True
            if seed_appeal_id and aid == int(seed_appeal_id) and step.get('kind') == 'seed_duplicate':
                group['is_seed'] = True

        sorted_aids = sorted(by_id.keys())
        total = len(sorted_aids)
        groups: list[dict] = []
        for idx, aid in enumerate(sorted_aids, start=1):
            group = by_id[aid]
            group['appeal_index'] = idx
            group['appeal_count'] = total
            group['player_summary'] = S1BugReclaimService._appeal_group_player_summary(group)
            group['system_verdict'] = S1BugReclaimService._appeal_group_system_verdict(
                group,
                issue_type=issue_type,
                fork_appeal_id=fork_appeal_id,
                seed_appeal_id=seed_appeal_id,
            )
            if group['is_illegal']:
                group['status_label'] = f'判非法 +{group["illegal_quantity"]}'
            elif group['is_fork']:
                group['status_label'] = '身份归位（合法）'
            elif group['is_seed']:
                group['status_label'] = '重复补发种子（合法）'
            else:
                group['status_label'] = '合法'
            groups.append(group)
        return groups

    @staticmethod
    def _appeal_grant_events(candidate: dict) -> list[dict]:
        tl = candidate.get('appeal_timeline') or {}
        steps = tl.get('steps') or []
        if steps:
            events = []
            for step in steps:
                if step.get('op_type') != 'create_item':
                    continue
                events.append({
                    'appeal_id': step.get('appeal_id'),
                    'audit_log_id': step.get('audit_log_id'),
                    'quantity': int(step.get('qty_after') or 1),
                    'name': step.get('name_after'),
                    'op_type': 'create_item',
                    'action': '新增物品',
                    'is_illegal': bool(step.get('is_illegal')),
                    'illegal_quantity': int(step.get('illegal_quantity') or 0),
                    'summary': step.get('summary_text') or S1BugReclaimService._human_step_summary(step),
                    'note': step.get('note'),
                })
            return events
        events = []
        for ev in candidate.get('lineage') or []:
            if ev.get('event_type') != 'create_item':
                continue
            events.append({
                'appeal_id': ev.get('appeal_id'),
                'audit_log_id': ev.get('audit_log_id'),
                'quantity': int(ev.get('new_quantity') or ev.get('illegal_quantity') or 0),
                'name': ev.get('name_after') or ev.get('name_before'),
                'op_type': 'create_item',
                'action': '新增物品',
                'is_illegal': True,
                'illegal_quantity': int(ev.get('illegal_quantity') or 0),
                'summary': f'新增物品「{ev.get("name_after") or ev.get("name_before") or ""}」',
            })
        return events

    @staticmethod
    def _pick_primary_stack(candidate: dict, stacks: list[dict]) -> dict | None:
        if not stacks:
            return None
        target_id = candidate.get('target_id')
        target_type = candidate.get('target_type')
        if target_id and target_type:
            for stack in stacks:
                if int(stack.get('id') or 0) == int(target_id) and stack.get('source') == target_type:
                    return stack
        return stacks[0]

    @staticmethod
    def _build_source_summary(stacks: list[dict], appeal_grants: list[dict]) -> str:
        parts: list[str] = []
        label_counts: dict[str, int] = {}
        for stack in stacks:
            label = stack.get('source_label') or '未知来源'
            label_counts[label] = label_counts.get(label, 0) + 1
        for label, count in sorted(label_counts.items(), key=lambda x: (-x[1], x[0])):
            parts.append(f'{label}×{count}堆' if count > 1 else label)
        grant_qty = sum(int(g.get('quantity') or 0) for g in appeal_grants)
        if grant_qty > 0:
            parts.append(f'申诉补发共 {grant_qty}')
        return '；'.join(parts) if parts else '未能从日志判定来源'

    @staticmethod
    def _list_matching_stacks(candidate: dict) -> list[dict]:
        adventurer_id = int(candidate['adventurer_id'])
        aliases = S1BugReclaimService._aliases(candidate)
        stacks: list[dict] = []
        seen_ids: set[tuple[str, int]] = set()
        for model, source in ((AdventurerS1WarehouseItem, 'warehouse'), (AdventurerS1Equipment, 'equipment')):
            rows = (
                db_manager.query(model)
                .filter(model.adventurer_id == adventurer_id, model.name.in_(aliases))
                .order_by(model.id.asc())
                .all()
            )
            for row in rows:
                token = (source, int(row.id))
                if token in seen_ids:
                    continue
                seen_ids.add(token)
                gain_log, skipped_logs = S1BugReclaimService._resolve_effective_gain_log(
                    adventurer_id,
                    row.name,
                    aliases,
                    getattr(row, 'created_at', None),
                )
                source_type = gain_log.source_type if gain_log else None
                stack_info = {
                    'source': source,
                    'id': row.id,
                    'name': row.name,
                    'custom_desc': S1BugReclaimService._truncate_text(getattr(row, 'custom_desc', None), 500),
                    'quantity': item_quantity_of(row),
                    'created_at': to_utc_iso(row.created_at) if getattr(row, 'created_at', None) else None,
                    'source_type': source_type,
                    'source_label': S1BugReclaimService._source_label(source_type),
                    'source_description': gain_log.description if gain_log else None,
                    'source_at': to_utc_iso(gain_log.created_at) if gain_log and gain_log.created_at else None,
                }
                if skipped_logs:
                    stack_info['source_trace'] = {
                        'skipped': [
                            S1BugReclaimService._format_gain_log_brief(s) for s in skipped_logs
                        ],
                        'effective': S1BugReclaimService._format_gain_log_brief(gain_log),
                    }
                stacks.append(stack_info)
        return stacks

    @staticmethod
    def _resolve_display_desc(candidate: dict, primary: dict | None) -> tuple[str | None, str | None]:
        """回显描述：现存堆 > 审计链 > 原描述。"""
        if primary and primary.get('custom_desc'):
            return (
                S1BugReclaimService._truncate_text(primary.get('custom_desc'), 500),
                '现存物品',
            )
        if candidate.get('current_desc'):
            return (
                S1BugReclaimService._truncate_text(candidate.get('current_desc'), 500),
                '申诉审计链',
            )
        for ev in reversed(candidate.get('lineage') or []):
            if ev.get('desc_after'):
                return (
                    S1BugReclaimService._truncate_text(ev.get('desc_after'), 500),
                    f"申诉 #{ev.get('appeal_id')} after",
                )
            if ev.get('desc_before') and ev.get('event_type') == 'quantity_update':
                return (
                    S1BugReclaimService._truncate_text(ev.get('desc_before'), 500),
                    f"申诉 #{ev.get('appeal_id')} 改量前",
                )
        if candidate.get('original_desc'):
            return (
                S1BugReclaimService._truncate_text(candidate.get('original_desc'), 500),
                '簇内最早快照',
            )
        return None, None

    @staticmethod
    def _quantity_analysis(candidate: dict) -> dict:
        stacks = S1BugReclaimService._list_matching_stacks(candidate)
        total = sum(int(s.get('quantity') or 0) for s in stacks)
        primary = S1BugReclaimService._pick_primary_stack(candidate, stacks)
        appeal_grants = S1BugReclaimService._appeal_grant_events(candidate)
        original_name = candidate.get('original_name') or candidate.get('item_name')
        original_desc = S1BugReclaimService._truncate_text(candidate.get('original_desc'), 500)
        current_name = (
            (primary.get('name') if primary else None)
            or candidate.get('current_name')
            or candidate.get('item_name')
        )
        display_desc, display_desc_source = S1BugReclaimService._resolve_display_desc(
            candidate, primary,
        )
        desc_cleaned = bool(candidate.get('desc_cleaned'))
        primary_source_type = primary.get('source_type') if primary else None
        primary_source_label = primary.get('source_label') if primary else None
        market_provenance = S1BugReclaimService._resolve_market_purchase_provenance(candidate)
        return {
            'original_name': original_name,
            'original_desc': original_desc,
            'current_name': current_name,
            'current_desc': display_desc,
            'display_desc_source': display_desc_source,
            'desc_cleaned': desc_cleaned,
            'origin_field_quantity': candidate.get('origin_field_quantity'),
            'current_stack_count': len(stacks),
            'current_total_quantity': total,
            'primary_source_type': primary_source_type,
            'primary_source_label': primary_source_label or '未能从日志判定来源',
            'primary_source_description': primary.get('source_description') if primary else None,
            'primary_source_at': primary.get('source_at') if primary else None,
            'primary_source_trace': primary.get('source_trace') if primary else None,
            'source_summary': S1BugReclaimService._build_source_summary(stacks, appeal_grants),
            'appeal_grants': appeal_grants,
            'stacks': stacks,
            'market_provenance': market_provenance,
        }

    @staticmethod
    def _cluster_key_target(adventurer_id: int, target_type: str, target_id: int) -> str:
        return f'target:{adventurer_id}:{target_type}:{target_id}'

    @staticmethod
    def _cluster_key_name(adventurer_id: int, normalized_name: str, item_type: str, rarity: str) -> str:
        return f'name:{adventurer_id}:{normalized_name}:{item_type}:{rarity}'

    @staticmethod
    def _audit_touches_cluster(
        audit,
        before: dict,
        after: dict,
        *,
        normalized: str,
        member_slots: set[tuple[str, int]],
    ) -> bool:
        if audit.op_type == 'create_item':
            return S1BugReclaimService._normalize_name(after.get('name')) == normalized
        if audit.op_type != 'update' or audit.target_type not in ('equipment', 'warehouse'):
            return False
        slot = S1BugReclaimService._target_slot(audit, before, after)
        if slot and (slot[1], slot[2]) in member_slots:
            return True
        for block in (before, after):
            if S1BugReclaimService._normalize_name(block.get('name')) == normalized:
                return True
        return False

    @staticmethod
    def _compose_appeal_timeline(
        cluster: dict,
        rows,
        lineages: dict[str, dict],
        fork_steps: dict[tuple[int, str, int], dict],
    ) -> dict:
        adv = int(cluster['adventurer_id'])
        norm = cluster['item_name']
        illegal_by_audit = {
            int(ev['audit_log_id']): ev
            for ev in cluster.get('lineage') or []
            if int(ev.get('illegal_quantity') or 0) > 0
        }
        legal_seed_by_audit = {
            int(ev['audit_log_id']): ev
            for ev in cluster.get('lineage') or []
            if int(ev.get('legal_seed_quantity') or 0) > 0
        }

        group = lineages.get(cluster.get('lineage_group_key') or '')
        if not group:
            for g in lineages.values():
                if int(g['adventurer_id']) == adv and g['normalized'] == norm:
                    group = g
                    break

        seed_appeal_id = int(group['seed_appeal_id']) if group else None
        fork_appeal_id = None
        fork_step = None
        for ev in cluster.get('lineage') or []:
            if ev.get('fork_appeal_id'):
                fork_appeal_id = int(ev['fork_appeal_id'])
                break
        if cluster.get('target_type') and cluster.get('target_id') is not None:
            slot = (adv, str(cluster['target_type']), int(cluster['target_id']))
            fork_step = fork_steps.get(slot)

        lineage_appeals = [int(ev['appeal_id']) for ev in cluster.get('lineage') or []]
        min_appeal = seed_appeal_id or fork_appeal_id or (min(lineage_appeals) if lineage_appeals else None)
        max_appeal = max(lineage_appeals) if lineage_appeals else min_appeal
        if min_appeal is None:
            return {'seed_appeal_id': seed_appeal_id, 'fork_appeal_id': fork_appeal_id, 'illegal_total': 0, 'steps': []}

        member_slots = set(group.get('members') or []) if group else set()
        if cluster.get('target_type') and cluster.get('target_id') is not None:
            member_slots.add((str(cluster['target_type']), int(cluster['target_id'])))

        steps: list[dict] = []
        seen_audit: set[int] = set()

        if fork_step and fork_appeal_id:
            steps.append({
                'kind': 'seed_fork',
                'appeal_id': fork_appeal_id,
                'audit_log_id': fork_step.get('audit_log_id'),
                'op_type': 'update',
                'target_type': fork_step.get('target_type'),
                'target_id': fork_step.get('target_id'),
                'name_before': fork_step.get('name_before'),
                'name_after': fork_step.get('name_after'),
                'desc_before': S1BugReclaimService._truncate_text(fork_step.get('desc_before'), 200),
                'desc_after': S1BugReclaimService._truncate_text(fork_step.get('desc_after'), 200),
                'qty_before': fork_step.get('qty_before'),
                'qty_after': fork_step.get('qty_after'),
                'is_illegal': False,
                'illegal_quantity': 0,
                'reason': None,
                'note': '同条写入 name + custom_desc，视为新身份归位',
            })
            seen_audit.add(int(fork_step['audit_log_id']))

        if group:
            for ss in group.get('seed_steps') or []:
                aid = int(ss['audit_log_id'])
                if aid in seen_audit:
                    continue
                seen_audit.add(aid)
                sib_text = '；'.join(
                    f"{s['target_type']}#{s['target_id']} ×{s['quantity']}「{s['name']}」"
                    for s in (ss.get('siblings') or [])
                )
                ev = illegal_by_audit.get(aid)
                steps.append({
                    'kind': 'seed_duplicate',
                    'appeal_id': int(ss['appeal_id']),
                    'audit_log_id': aid,
                    'op_type': 'create_item',
                    'target_type': ss.get('target_type'),
                    'target_id': ss.get('target_id'),
                    'name_after': ss.get('create_name'),
                    'qty_after': ss.get('create_quantity'),
                    'is_illegal': bool(ev),
                    'illegal_quantity': int(ev['illegal_quantity']) if ev else 0,
                    'reason': ev.get('reason') if ev else None,
                    'siblings': ss.get('siblings') or [],
                    'note': f'快照已有同名：{sib_text}' if sib_text else '快照已有同名物品',
                })

        for audit, appeal in rows:
            if int(audit.adventurer_id) != adv:
                continue
            appeal_id = int(appeal.id)
            if appeal_id < min_appeal or (max_appeal and appeal_id > max_appeal):
                continue
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if not S1BugReclaimService._audit_touches_cluster(
                audit, before, after, normalized=norm, member_slots=member_slots,
            ):
                continue
            if audit.id in seen_audit:
                continue
            seen_audit.add(int(audit.id))

            ev = illegal_by_audit.get(int(audit.id))
            seed_ev = legal_seed_by_audit.get(int(audit.id))
            before = before or {}
            after = after or {}
            steps.append({
                'kind': 'illegal_followup' if ev else 'legal_seed' if seed_ev else 'context',
                'appeal_id': appeal_id,
                'audit_log_id': audit.id,
                'op_type': audit.op_type,
                'target_type': audit.target_type,
                'target_id': audit.target_id,
                'name_before': before.get('name'),
                'name_after': after.get('name'),
                'desc_before': S1BugReclaimService._truncate_text(before.get('custom_desc'), 200),
                'desc_after': S1BugReclaimService._truncate_text(after.get('custom_desc'), 200),
                'qty_before': before.get('quantity'),
                'qty_after': after.get('quantity'),
                'is_illegal': bool(ev),
                'illegal_quantity': int(ev['illegal_quantity']) if ev else 0,
                'reason': ev.get('reason') if ev else None,
                'legal_seed_quantity': int(seed_ev.get('legal_seed_quantity') or 0) if seed_ev else 0,
                'note': seed_ev.get('note') if seed_ev else None,
                'applied_at': to_utc_iso(appeal.applied_at) if appeal.applied_at else None,
            })

        steps.sort(key=lambda s: (int(s['appeal_id']), int(s['audit_log_id'] or 0)))

        for step in steps:
            step['summary_text'] = S1BugReclaimService._human_step_summary(step)
            step['action_label'] = (
                '新增物品' if step.get('op_type') == 'create_item'
                else '改数量/字段' if step.get('op_type') == 'update'
                else step.get('op_type')
            )
            qb, qa = step.get('qty_before'), step.get('qty_after')
            if qb is not None and (qa is None or qa == qb):
                step['qty_unchanged_note'] = f'quantity 未改（仍为 {int(qb)}）'

        appeal_groups = S1BugReclaimService._build_appeal_groups(
            steps,
            fork_appeal_id=fork_appeal_id,
            seed_appeal_id=seed_appeal_id,
            issue_type=str(cluster.get('issue_type') or ''),
        )

        return {
            'seed_appeal_id': seed_appeal_id,
            'fork_appeal_id': fork_appeal_id,
            'illegal_total': int(cluster.get('illegal_quantity') or 0),
            'appeal_count': len(appeal_groups),
            'illegal_appeal_count': sum(1 for g in appeal_groups if g.get('is_illegal')),
            'verdict_text': S1BugReclaimService._issue_verdict_text(cluster, seed_appeal_id, fork_appeal_id),
            'appeal_groups': appeal_groups,
            'steps': steps,
        }

    @staticmethod
    def _approved_audit_query():
        return (
            db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
            .join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id)
            .filter(
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                or_(
                    AdventurerS1DataAppealAuditLog.op_type == 'create_item',
                    and_(
                        AdventurerS1DataAppealAuditLog.op_type.in_(('update', 'delete')),
                        AdventurerS1DataAppealAuditLog.target_type.in_(
                            ('equipment', 'warehouse'),
                        ),
                    ),
                ),
            )
        )

    @staticmethod
    def _fetch_approved_audit_rows(
        *,
        after_id: int | None = None,
        through_id: int | None = None,
        adventurer_ids: set[int] | None = None,
        include_catalog_text: bool = False,
        include_source_snapshots: bool = True,
    ) -> list[tuple]:
        if adventurer_ids is not None and not adventurer_ids:
            return []
        query = S1BugReclaimService._approved_audit_query()
        if after_id is not None:
            query = query.filter(AdventurerS1DataAppealAuditLog.id > int(after_id))
        if adventurer_ids is not None:
            query = query.filter(AdventurerS1DataAppealAuditLog.adventurer_id.in_(adventurer_ids))
        audit_model = AdventurerS1DataAppealAuditLog
        appeal_model = AdventurerS1DataAppeal
        if through_id is None:
            # A catalog/full-history read must not chase newly appended audits.
            through_id = query.with_entities(func.max(audit_model.id)).scalar()
            if through_id is None:
                return []
        query = query.filter(audit_model.id <= int(through_id))

        # Selecting ORM entities also reads/decompresses every appeal evidence,
        # raw AI response and review payload, repeated for each joined audit.
        # Materialize only consumer fields; no ORM/lazy reads escape this phase.
        audit_fields = (
            'id', 'adventurer_id', 'op_type', 'target_type', 'target_id',
            'before_json', 'after_json', 'created_at',
        )
        appeal_fields = ('id', 'user_id', 'expedition_log_id', 'created_at', 'applied_at')
        if include_catalog_text:
            audit_fields += ('note',)
            appeal_fields += ('player_note', 'ai_summary')
        query = query.with_entities(
            *(getattr(audit_model, name).label('audit_' + name) for name in audit_fields),
            *(getattr(appeal_model, name).label('appeal_' + name) for name in appeal_fields),
        )
        page_size = S1BugReclaimService._scan_audit_row_budget()
        cursor = int(after_id or 0)
        snapshots: dict[int, Optional[dict]] = {}
        rows = []
        while True:
            page = (
                query.filter(audit_model.id > cursor)
                .order_by(audit_model.id.asc())
                .limit(page_size)
                .all()
            )
            if not page:
                break
            # Only create_item reconstruction uses the pre-appeal inventory.
            # Fetch it once per appeal, even if its audits span multiple pages.
            snapshot_ids = {
                row.appeal_id for row in page
                if include_source_snapshots
                and row.audit_op_type == 'create_item'
                and row.appeal_id not in snapshots
            }
            if snapshot_ids:
                snapshots.update(
                    db_manager.query(appeal_model.id, appeal_model.snapshot_before)
                    .filter(appeal_model.id.in_(snapshot_ids))
                    .all()
                )
                if snapshot_ids.difference(snapshots):
                    raise ValueError('Reclaim appeal disappeared during history read')
            for row in page:
                audit = _ReclaimAuditSnapshot(**{
                    name: getattr(row, 'audit_' + name) for name in audit_fields
                })
                appeal = _ReclaimAppealSnapshot(
                    **{name: getattr(row, 'appeal_' + name) for name in appeal_fields},
                    snapshot_before=snapshots.get(row.appeal_id),
                )
                rows.append((audit, appeal))
            cursor = int(page[-1].audit_id)
            if len(page) < page_size:
                break
        return rows

    @staticmethod
    def _max_approved_audit_id() -> int:
        value = (
            S1BugReclaimService._approved_audit_query()
            .with_entities(func.max(AdventurerS1DataAppealAuditLog.id))
            .scalar()
        )
        return int(value or 0)

    @staticmethod
    def _scan_source_adventurer_query(
        *,
        after_audit_id: int,
        through_audit_id: int,
    ):
        return (
            S1BugReclaimService._approved_audit_query()
            .with_entities(AdventurerS1DataAppealAuditLog.adventurer_id)
            .filter(
                AdventurerS1DataAppealAuditLog.id > int(after_audit_id),
                AdventurerS1DataAppealAuditLog.id <= int(through_audit_id),
            )
        )

    @staticmethod
    def _scan_source_adventurer_count(
        *,
        after_audit_id: int,
        through_audit_id: int,
    ) -> int:
        if int(through_audit_id) <= int(after_audit_id):
            return 0
        value = (
            S1BugReclaimService._scan_source_adventurer_query(
                after_audit_id=after_audit_id,
                through_audit_id=through_audit_id,
            )
            .with_entities(func.count(func.distinct(
                AdventurerS1DataAppealAuditLog.adventurer_id,
            )))
            .scalar()
        )
        return int(value or 0)

    @staticmethod
    def _next_scan_source_adventurer_ids(
        *,
        after_audit_id: int,
        through_audit_id: int,
        after_adventurer_id: int,
        limit: int,
    ) -> list[int]:
        if int(through_audit_id) <= int(after_audit_id):
            return []
        rows = (
            S1BugReclaimService._scan_source_adventurer_query(
                after_audit_id=after_audit_id,
                through_audit_id=through_audit_id,
            )
            .filter(
                AdventurerS1DataAppealAuditLog.adventurer_id
                > int(after_adventurer_id),
            )
            .distinct()
            .order_by(AdventurerS1DataAppealAuditLog.adventurer_id.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        return [int(row[0]) for row in rows if row and int(row[0] or 0) > 0]

    @staticmethod
    def _build_cluster_candidates(excluded: set[str], rows=None) -> tuple[list[dict], dict]:
        """申诉 audit 中 quantity 增加 + 重复补发 lineage 聚类。"""
        if rows is None:
            rows = S1BugReclaimService._fetch_approved_audit_rows()
        appeal_deleted = S1BugReclaimService._appeal_deleted_slots(rows)
        quantity_net_by_appeal_slot: dict[tuple[int, tuple[int, str, int]], dict] = {}
        for audit, appeal in rows:
            if audit.op_type != 'update' or audit.target_type not in ('equipment', 'warehouse'):
                continue
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if 'quantity' not in after:
                continue
            slot = S1BugReclaimService._target_slot(audit, before, after)
            if not slot:
                continue
            net_key = (int(appeal.id), slot)
            old_qty = S1BugReclaimService._qty(before.get('quantity'))
            new_qty = S1BugReclaimService._qty(after.get('quantity'))
            info = quantity_net_by_appeal_slot.setdefault(net_key, {
                'start': old_qty,
                'end': old_qty,
                'counted': 0,
            })
            info['end'] = new_qty

        fork_appeal: dict[tuple[int, str, int], int] = {}
        fork_steps: dict[tuple[int, str, int], dict] = {}
        lineages: dict[str, dict] = {}

        for audit, appeal in rows:
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            appeal_id = int(appeal.id)
            adventurer_id = int(audit.adventurer_id)

            if audit.op_type == 'update' and audit.target_type in ('equipment', 'warehouse'):
                slot = S1BugReclaimService._target_slot(audit, before, after)
                if slot and S1BugReclaimService._is_identity_fork(before, after):
                    fork_appeal[slot] = min(fork_appeal.get(slot, appeal_id), appeal_id)
                    fork_steps[slot] = {
                        'appeal_id': fork_appeal[slot],
                        'audit_log_id': audit.id,
                        'target_type': slot[1],
                        'target_id': slot[2],
                        'name_before': before.get('name'),
                        'name_after': after.get('name'),
                        'desc_before': before.get('custom_desc'),
                        'desc_after': after.get('custom_desc'),
                        'qty_before': before.get('quantity'),
                        'qty_after': after.get('quantity'),
                    }
                continue

            if audit.op_type != 'create_item':
                continue

            item_name = S1BugReclaimService._text(after.get('name'))
            if not item_name:
                continue
            normalized = S1BugReclaimService._normalize_name(item_name)
            item_type = str(after.get('type') or '')
            rarity = str(after.get('rarity') or 'common')
            snapshot = appeal.snapshot_before if isinstance(appeal.snapshot_before, dict) else {}
            siblings = S1BugReclaimService._snapshot_siblings(
                snapshot,
                normalized,
                appeal_deleted.get(appeal_id, set()),
            )
            if not siblings:
                continue
            appeal_source_key = S1BugReclaimService._appeal_source_key(appeal)
            group_key = S1BugReclaimService._lineage_group_key(
                adventurer_id, normalized, item_type, rarity, appeal_source_key,
            )
            group = lineages.setdefault(group_key, {
                'group_key': group_key,
                'seed_appeal_id': appeal_id,
                'adventurer_id': adventurer_id,
                'normalized': normalized,
                'item_type': item_type,
                'item_rarity': rarity,
                'appeal_source_key': appeal_source_key,
                'members': set(),
                'seed_steps': [],
            })
            group['seed_appeal_id'] = min(int(group['seed_appeal_id']), appeal_id)
            for sibling in siblings:
                group['members'].add((str(sibling.get('section')), int(sibling.get('id'))))
            group['seed_steps'].append({
                'appeal_id': appeal_id,
                'audit_log_id': audit.id,
                'create_name': item_name,
                'create_quantity': S1BugReclaimService._qty(after.get('quantity')),
                'siblings': [
                    {
                        'target_type': str(s.get('section') or ''),
                        'target_id': int(s.get('id')),
                        'name': s.get('name'),
                        'quantity': S1BugReclaimService._qty(s.get('quantity')),
                        'desc': S1BugReclaimService._truncate_text(s.get('custom_desc'), 120),
                    }
                    for s in siblings
                ],
            })

        clusters: dict[str, dict] = {}

        def _ensure_cluster(
            key: str,
            *,
            issue_type: str,
            adventurer_id: int,
            user_id,
            item_name: str,
            item_type: str,
            rarity: str,
            target_type: str | None = None,
            target_id: int | None = None,
        ):
            if key not in clusters:
                clusters[key] = {
                    'issue_key': f'cluster:{key}',
                    'issue_type': issue_type,
                    'adventurer_id': adventurer_id,
                    'user_id': user_id,
                    'item_name': item_name,
                    'item_type': item_type or '',
                    'item_rarity': rarity or 'common',
                    'target_type': target_type,
                    'target_id': target_id,
                    'illegal_quantity': 0,
                    'lineage': [],
                    'name_aliases': set(),
                    '_since_candidates': [],
                    'appeal_id': None,
                    'audit_log_id': None,
                    'before': {},
                    'after': {},
                }
            return clusters[key]

        def _append_event(
            cluster: dict,
            *,
            appeal,
            audit,
            illegal_qty: int,
            reason: str,
            before: dict,
            after: dict,
            event_type: str,
            fork_appeal_id: int | None = None,
            seed_appeal_id: int | None = None,
        ):
            if illegal_qty <= 0:
                return
            since = appeal.applied_at or audit.created_at or appeal.created_at
            cluster['illegal_quantity'] += illegal_qty
            cluster['lineage'].append({
                'event_type': event_type,
                'reason': reason,
                'illegal_quantity': illegal_qty,
                'computed_quantity': illegal_qty,
                'appeal_id': appeal.id,
                'audit_log_id': audit.id,
                'fork_appeal_id': fork_appeal_id,
                'seed_appeal_id': seed_appeal_id,
                'old_quantity': S1BugReclaimService._qty(before.get('quantity')) if before else None,
                'new_quantity': S1BugReclaimService._qty(after.get('quantity')) if after else None,
                'name_before': before.get('name') if before else None,
                'name_after': after.get('name') if after else None,
                'desc_before': before.get('custom_desc') if before else None,
                'desc_after': after.get('custom_desc') if after else None,
                'applied_at': to_utc_iso(appeal.applied_at) if appeal.applied_at else None,
                'audit_at': to_utc_iso(audit.created_at) if audit.created_at else None,
            })
            cluster['_since_candidates'].append(since)
            cluster['appeal_id'] = appeal.id
            cluster['audit_log_id'] = audit.id
            cluster['before'] = before or cluster['before']
            cluster['after'] = after or cluster['after']
            for block in (before or {}, after or {}):
                name = block.get('name')
                if name:
                    cluster['name_aliases'].add(str(name).strip())
                    cluster['name_aliases'].add(S1BugReclaimService._normalize_name(name))

        def _touch_cluster_meta(cluster: dict, before: dict, after: dict) -> None:
            S1BugReclaimService._record_cluster_origin(cluster, before)
            S1BugReclaimService._record_cluster_current(cluster, before, after)

        dup_create_audit_ids: set[int] = set()

        for audit, appeal in rows:
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            appeal_id = int(appeal.id)
            adventurer_id = int(audit.adventurer_id)

            if audit.op_type == 'update' and audit.target_type in ('equipment', 'warehouse'):
                if 'quantity' not in after:
                    continue
                old_qty = S1BugReclaimService._qty(before.get('quantity'))
                new_qty = S1BugReclaimService._qty(after.get('quantity'))
                if new_qty <= old_qty:
                    continue
                slot = S1BugReclaimService._target_slot(audit, before, after)
                if not slot:
                    continue
                net_info = quantity_net_by_appeal_slot.get((appeal_id, slot)) or {}
                net_delta = max(0, int(net_info.get('end') or old_qty) - int(net_info.get('start') or old_qty))
                remaining_net = net_delta - int(net_info.get('counted') or 0)
                if remaining_net <= 0:
                    continue
                illegal_qty = min(new_qty - old_qty, remaining_net)
                net_info['counted'] = int(net_info.get('counted') or 0) + illegal_qty
                key, normalized, item_type, rarity, target_id, target_type = (
                    S1BugReclaimService._resolve_update_cluster_key(audit, before, after)
                )
                if not key:
                    continue

                fork_id = fork_appeal.get(slot) if slot else None
                issue_type = 'appeal_qty_increase'
                reason = 'appeal_qty_increase'
                cluster_key = key
                fork_appeal_id = None
                seed_appeal_id = None
                lineage_group_key = None

                if fork_id and appeal_id > fork_id:
                    issue_type = 'identity_reappeal'
                    reason = 'identity_reappeal_qty'
                    fork_appeal_id = fork_id

                item_norm = S1BugReclaimService._normalize_name(after.get('name', before.get('name')))
                appeal_source_key = S1BugReclaimService._appeal_source_key(appeal)
                for group in lineages.values():
                    if int(group['adventurer_id']) != adventurer_id:
                        continue
                    if (
                        group.get('appeal_source_key')
                        and appeal_source_key
                        and group['appeal_source_key'] != appeal_source_key
                    ):
                        continue
                    seed_id = int(group['seed_appeal_id'])
                    if appeal_id <= seed_id:
                        continue
                    if item_norm != group['normalized'] and (not slot or slot not in group['members']):
                        continue
                    cluster_key = group['group_key']
                    lineage_group_key = cluster_key
                    seed_appeal_id = seed_id
                    if issue_type == 'identity_reappeal':
                        issue_type = 'mixed_lineage'
                        reason = 'duplicate_lineage_qty'
                    else:
                        issue_type = 'duplicate_lineage_reappeal'
                        reason = 'duplicate_lineage_qty'
                    break

                cluster = _ensure_cluster(
                    cluster_key,
                    issue_type=issue_type,
                    adventurer_id=adventurer_id,
                    user_id=appeal.user_id,
                    item_name=normalized if cluster_key == key else (
                        lineages.get(cluster_key, {}).get('normalized') or normalized
                    ),
                    item_type=item_type if cluster_key == key else (
                        lineages.get(cluster_key, {}).get('item_type') or item_type
                    ),
                    rarity=rarity if cluster_key == key else (
                        lineages.get(cluster_key, {}).get('item_rarity') or rarity
                    ),
                    target_type=target_type,
                    target_id=target_id,
                )
                if lineage_group_key:
                    cluster['lineage_group_key'] = lineage_group_key
                    cluster['appeal_source_key'] = appeal_source_key
                _touch_cluster_meta(cluster, before, after)
                _append_event(
                    cluster,
                    appeal=appeal,
                    audit=audit,
                    illegal_qty=illegal_qty,
                    reason=reason,
                    before=before,
                    after=after,
                    event_type='quantity_update',
                    fork_appeal_id=fork_appeal_id,
                    seed_appeal_id=seed_appeal_id,
                )
                continue

            if audit.op_type != 'create_item':
                continue

            item_name = S1BugReclaimService._text(after.get('name'))
            if not item_name:
                continue
            normalized = S1BugReclaimService._normalize_name(item_name)
            create_qty = S1BugReclaimService._qty(after.get('quantity'))
            item_type = str(after.get('type') or '')
            rarity = str(after.get('rarity') or 'common')
            appeal_source_key = S1BugReclaimService._appeal_source_key(appeal)

            for group in lineages.values():
                if int(group['adventurer_id']) != adventurer_id:
                    continue
                if (
                    group.get('appeal_source_key')
                    and appeal_source_key
                    and group['appeal_source_key'] != appeal_source_key
                ):
                    continue
                seed_id = int(group['seed_appeal_id'])
                if appeal_id <= seed_id or normalized != group['normalized']:
                    continue
                dup_key = group['group_key']
                cluster = _ensure_cluster(
                    dup_key,
                    issue_type='duplicate_lineage_reappeal',
                    adventurer_id=adventurer_id,
                    user_id=appeal.user_id,
                    item_name=normalized,
                    item_type=item_type or group['item_type'],
                    rarity=rarity or group['item_rarity'],
                )
                cluster['lineage_group_key'] = dup_key
                cluster['appeal_source_key'] = appeal_source_key
                _touch_cluster_meta(cluster, {}, after)
                dup_create_audit_ids.add(int(audit.id))
                _append_event(
                    cluster,
                    appeal=appeal,
                    audit=audit,
                    illegal_qty=create_qty,
                    reason='duplicate_lineage_create',
                    before={},
                    after=after,
                    event_type='create_item',
                    seed_appeal_id=seed_id,
                )

            create_qty_ci = S1BugReclaimService._qty(after.get('quantity'))
            if int(audit.id) in dup_create_audit_ids:
                continue
            grant_key = S1BugReclaimService._cluster_key_name(
                adventurer_id, normalized, item_type, rarity,
            )
            if appeal_source_key:
                grant_key = f'{grant_key}:source:{appeal_source_key}'
            grant_cluster = _ensure_cluster(
                grant_key,
                issue_type='appeal_create_grant',
                adventurer_id=adventurer_id,
                user_id=appeal.user_id,
                item_name=normalized,
                item_type=item_type,
                rarity=rarity,
            )
            _touch_cluster_meta(grant_cluster, {}, after)
            grant_cluster['appeal_source_key'] = appeal_source_key
            _append_event(
                grant_cluster,
                appeal=appeal,
                audit=audit,
                illegal_qty=create_qty_ci,
                reason='appeal_create_grant',
                before={},
                after=after,
                event_type='create_item',
            )

        S1BugReclaimService._mark_first_plain_appeal_gain_as_legal_seed(clusters)

        for cluster in clusters.values():
            if cluster['illegal_quantity'] <= 0:
                continue
            cluster['appeal_timeline'] = S1BugReclaimService._compose_appeal_timeline(
                cluster, rows, lineages, fork_steps,
            )

        candidates: list[dict] = []
        for cluster in clusters.values():
            if cluster['illegal_quantity'] <= 0:
                continue
            issue_key = cluster['issue_key']
            if issue_key in excluded:
                continue
            since_list = [s for s in cluster['_since_candidates'] if s is not None]
            since = min(since_list) if since_list else None
            aliases = {a for a in cluster['name_aliases'] if a}
            aliases.add(cluster['item_name'])
            candidates.append({
                'issue_key': issue_key,
                'issue_type': cluster['issue_type'],
                'appeal_id': cluster['appeal_id'],
                'audit_log_id': cluster['audit_log_id'],
                'adventurer_id': cluster['adventurer_id'],
                'user_id': cluster['user_id'],
                'item_name': cluster['item_name'],
                'item_type': cluster['item_type'],
                'item_rarity': cluster['item_rarity'],
                'illegal_quantity': int(cluster['illegal_quantity']),
                'target_type': cluster.get('target_type'),
                'target_id': cluster.get('target_id'),
                'original_name': cluster.get('original_name'),
                'original_desc': cluster.get('original_desc'),
                'origin_field_quantity': cluster.get('origin_field_quantity'),
                'current_name': cluster.get('current_name'),
                'current_desc': cluster.get('current_desc'),
                'desc_cleaned': bool(cluster.get('desc_cleaned')),
                'lineage': cluster['lineage'],
                'lineage_count': len(cluster['lineage']),
                'legal_seed_quantity': int(cluster.get('legal_seed_quantity') or 0),
                'prior_legal_seed': cluster.get('prior_legal_seed'),
                'appeal_timeline': cluster.get('appeal_timeline') or {},
                'before': cluster['before'],
                'after': {
                    **(cluster['after'] or {}),
                    'lineage': cluster['lineage'],
                },
                'created_at': cluster['lineage'][-1]['audit_at'] if cluster['lineage'] else None,
                'applied_at': cluster['lineage'][-1]['applied_at'] if cluster['lineage'] else None,
                '_since': since,
                '_name_aliases': aliases,
                '_appeal_source_key': cluster.get('appeal_source_key'),
            })

        candidates.sort(key=lambda c: (-int(c['illegal_quantity']), c['issue_key']))
        scan_meta = {
            'audit_rows': len(rows),
            'identity_fork_slots': len(fork_steps),
            'duplicate_lineage_groups': len(lineages),
            'cluster_count': len(candidates),
        }
        return candidates, scan_meta

    @staticmethod
    def _adventurer_has_create_item_audit(
        adventurer_id: int,
        normalized: str,
        aliases: set[str],
        rows,
    ) -> bool:
        names = {S1BugReclaimService._normalize_name(normalized)}
        for alias in aliases or set():
            if alias:
                names.add(S1BugReclaimService._normalize_name(alias))
        for audit, _appeal in rows:
            if int(audit.adventurer_id) != int(adventurer_id):
                continue
            if audit.op_type != 'create_item':
                continue
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if S1BugReclaimService._normalize_name(after.get('name')) in names:
                return True
        return False

    @staticmethod
    def _lineage_create_item_steps(lineage: list[dict]) -> list[dict]:
        return [
            ev for ev in (lineage or [])
            if ev.get('event_type') == 'create_item' and int(ev.get('illegal_quantity') or 0) > 0
        ]

    @staticmethod
    def _seller_had_prior_appeal_gain(seller_id: int, item_name: str, before_dt) -> bool:
        normalized = S1BugReclaimService._normalize_name(item_name)
        rows = (
            db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
            .join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id)
            .filter(
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                AdventurerS1DataAppealAuditLog.adventurer_id == int(seller_id),
                AdventurerS1DataAppealAuditLog.op_type.in_(('update', 'create_item')),
            )
            .order_by(AdventurerS1DataAppealAuditLog.id.asc())
            .all()
        )
        for audit, appeal in rows:
            applied = appeal.applied_at or audit.created_at or appeal.created_at
            if before_dt and applied and applied > before_dt:
                continue
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if audit.op_type == 'create_item':
                if S1BugReclaimService._normalize_name(after.get('name')) == normalized:
                    return True
                continue
            if audit.target_type not in ('equipment', 'warehouse'):
                continue
            touched = any(
                S1BugReclaimService._normalize_name(block.get('name')) == normalized
                for block in (before, after)
            )
            if not touched or 'quantity' not in after:
                continue
            if S1BugReclaimService._qty(after.get('quantity')) > S1BugReclaimService._qty(before.get('quantity')):
                return True
        return False

    @staticmethod
    def _is_market_downstream_quantity_seed(cluster: dict, event: dict) -> bool:
        """买方市场购入堆若来自卖方申诉补量链路，后续改量不能享受首修豁免。"""
        if cluster.get('target_type') != 'warehouse' or cluster.get('target_id') is None:
            return False
        adventurer_id = int(cluster.get('adventurer_id') or 0)
        if adventurer_id <= 0:
            return False
        item_name = event.get('name_after') or event.get('name_before') or cluster.get('item_name')
        if not item_name:
            return False
        audit = db_manager.query(AdventurerS1DataAppealAuditLog).filter_by(
            id=int(event.get('audit_log_id') or 0),
        ).first()
        audit_dt = audit.created_at if audit else None
        gain_log, _skipped = S1BugReclaimService._resolve_effective_gain_log(
            adventurer_id,
            str(item_name),
            {str(item_name), S1BugReclaimService._normalize_name(item_name)},
            audit_dt,
        )
        if not gain_log or gain_log.source_type != 'market':
            return False
        if not gain_log.related_id or not str(gain_log.related_id).isdigit():
            return False
        listing = (
            db_manager.query(AdventurerS1MarketListing)
            .filter_by(id=int(gain_log.related_id))
            .first()
        )
        if not listing or int(listing.buyer_adventurer_id or 0) != adventurer_id:
            return False
        return S1BugReclaimService._seller_had_prior_appeal_gain(
            int(listing.seller_adventurer_id),
            listing.item_name,
            listing.completed_at or gain_log.created_at,
        )

    @staticmethod
    def _market_downstream_reappeal_flow(candidate: dict) -> dict | None:
        """候选本身是买方市场购入后再申诉改量时，直接标为下游再申诉。"""
        if candidate.get('target_type') != 'warehouse' or candidate.get('target_id') is None:
            return None
        adventurer_id = int(candidate.get('adventurer_id') or 0)
        if adventurer_id <= 0:
            return None
        audit_dt = candidate.get('_since')
        gain_log, _skipped = S1BugReclaimService._resolve_effective_gain_log(
            adventurer_id,
            str(candidate.get('item_name') or ''),
            S1BugReclaimService._aliases(candidate),
            audit_dt,
        )
        if not gain_log or gain_log.source_type != 'market':
            return None
        if not gain_log.related_id or not str(gain_log.related_id).isdigit():
            return None
        listing = (
            db_manager.query(AdventurerS1MarketListing)
            .filter_by(id=int(gain_log.related_id))
            .first()
        )
        if not listing or int(listing.buyer_adventurer_id or 0) != adventurer_id:
            return None
        if not S1BugReclaimService._seller_had_prior_appeal_gain(
            int(listing.seller_adventurer_id),
            listing.item_name,
            listing.completed_at or gain_log.created_at,
        ):
            return None
        appeal_ids = sorted(S1BugReclaimService._cluster_lineage_appeal_ids(candidate))
        seller = db_manager.query(AdventurerS1).filter_by(id=int(listing.seller_adventurer_id)).first()
        seller_label = seller.name if seller else f'#{int(listing.seller_adventurer_id)}'
        illegal_qty = int(candidate.get('illegal_quantity') or 0)
        return {
            'should_reclaim': True,
            'verdict': 'downstream_buyer_reappeal',
            'chain_role': 'downstream_buyer',
            'label': S1BugReclaimService._flow_verdict_label('downstream_buyer_reappeal'),
            'detail': (
                f'非源头：市场购入自 {seller_label}（#{int(listing.seller_adventurer_id)}），'
                f'卖方此前已申诉补量；买方申诉改量 +{illegal_qty} 全部认定非法'
                f'（不享首次归正豁免，按扣物/扣金回收）。'
            ),
            'upstream_seller_adventurer_id': int(listing.seller_adventurer_id),
            'upstream_seller_name': seller_label,
            'upstream_listing_id': int(listing.id),
            'appeal_ids': appeal_ids,
            'self_used_quantity': 0,
            'transferred_quantity': int(candidate.get('illegal_quantity') or 0),
            'downstream_appeals': [{
                'buyer_adventurer_id': adventurer_id,
                'buyer_appeal_ids': appeal_ids,
                'listing_id': int(listing.id),
                'listing_type': listing.listing_type,
                'completed_at': listing.completed_at or gain_log.created_at,
                'item_name': listing.item_name,
                'quantity': int(gain_log.quantity or 1),
            }],
        }

    @staticmethod
    def _mark_first_plain_appeal_gain_as_legal_seed(clusters: dict[str, dict]) -> None:
        """纯改量链路按目标记录隔离：每条记录首次 quantity 归一视为合法。"""
        for cluster in clusters.values():
            if cluster.get('issue_type') != 'appeal_qty_increase':
                continue
            first_event = None
            for event in cluster.get('lineage') or []:
                if event.get('event_type') == 'create_item':
                    continue
                qty = int(event.get('illegal_quantity') or 0)
                if qty <= 0:
                    continue
                token = (int(event.get('appeal_id') or 0), int(event.get('audit_log_id') or 0))
                if first_event is None:
                    first_event = event
                    continue
                first_token = (
                    int(first_event.get('appeal_id') or 0),
                    int(first_event.get('audit_log_id') or 0),
                )
                if token < first_token:
                    first_event = event

            if first_event is None:
                continue
            if S1BugReclaimService._is_market_downstream_quantity_seed(cluster, first_event):
                continue
            qty = int(first_event.get('illegal_quantity') or 0)
            if qty <= 0:
                continue
            cluster['illegal_quantity'] = max(0, int(cluster.get('illegal_quantity') or 0) - qty)
            cluster['legal_seed_quantity'] = int(cluster.get('legal_seed_quantity') or 0) + qty
            first_event['legal_seed_quantity'] = qty
            first_event['illegal_quantity'] = 0
            first_event['reason'] = 'first_quantity_normalization_seed'
            first_event['note'] = '当前目标记录首次申诉数量归一，视为合法修正，不回收'
            seed_meta = {
                'appeal_id': first_event.get('appeal_id'),
                'audit_log_id': first_event.get('audit_log_id'),
                'quantity': qty,
                'target_type': cluster.get('target_type'),
                'target_id': cluster.get('target_id'),
            }
            seed_appeal_id = int(seed_meta.get('appeal_id') or 0)
            for event in cluster.get('lineage') or []:
                if int(event.get('illegal_quantity') or 0) <= 0:
                    continue
                if int(event.get('appeal_id') or 0) <= seed_appeal_id:
                    continue
                cluster['prior_legal_seed'] = seed_meta
                break

    @staticmethod
    def _current_items(candidate: dict, *, all_matching: bool = False) -> list:
        """回收扣物时 all_matching=True，按同名/别名汇总所有现存堆，不限于申诉 target_id。"""
        target_id = candidate.get('target_id')
        target_type = candidate.get('target_type')
        adventurer_id = int(candidate['adventurer_id'])
        if not all_matching and target_id and target_type in ('warehouse', 'equipment'):
            model = AdventurerS1WarehouseItem if target_type == 'warehouse' else AdventurerS1Equipment
            row = (
                db_manager.query(model)
                .filter(model.id == int(target_id), model.adventurer_id == adventurer_id)
                .first()
            )
            if row:
                return [(target_type, row)]

        aliases = S1BugReclaimService._aliases(candidate)
        rows = []
        for model, source in ((AdventurerS1WarehouseItem, 'warehouse'), (AdventurerS1Equipment, 'equipment')):
            q = (
                db_manager.query(model)
                .filter(model.adventurer_id == adventurer_id, model.name.in_(aliases))
                .order_by(model.id.asc())
                .all()
            )
            rows.extend((source, r) for r in q)
        return rows

    @staticmethod
    def _aliases(candidate: dict) -> set[str]:
        aliases = set(candidate.get('_name_aliases') or [])
        aliases.add(candidate['item_name'])
        for block in (candidate.get('before') or {}, candidate.get('after') or {}):
            if not isinstance(block, dict):
                continue
            name = block.get('name') or block.get('resolved_name')
            if name:
                aliases.add(str(name).strip())
                aliases.add(S1BugReclaimService._normalize_name(name))
        return {a for a in aliases if a}

    @staticmethod
    def _audit_touches_normalized(audit, normalized: str, aliases: set[str] | None = None) -> bool:
        before = audit.before_json if isinstance(audit.before_json, dict) else {}
        after = audit.after_json if isinstance(audit.after_json, dict) else {}
        names = {S1BugReclaimService._normalize_name(normalized)}
        for alias in aliases or set():
            if alias:
                names.add(str(alias).strip())
                names.add(S1BugReclaimService._normalize_name(alias))
        if audit.op_type == 'create_item':
            return S1BugReclaimService._normalize_name(after.get('name')) in names
        if audit.op_type == 'update':
            for block in (before, after):
                if S1BugReclaimService._normalize_name(block.get('name')) in names:
                    return True
        return False

    @staticmethod
    def _later_appeal_ids_on_item(
        adventurer_id: int,
        normalized: str,
        aliases: set[str],
        after_dt,
        rows,
        *,
        after_appeal_id: int | None = None,
    ) -> list[int]:
        hits: list[int] = []
        for audit, appeal in rows:
            if int(audit.adventurer_id) != int(adventurer_id):
                continue
            appeal_id = int(appeal.id)
            if after_appeal_id is not None and appeal_id <= int(after_appeal_id):
                continue
            applied = appeal.applied_at or audit.created_at or appeal.created_at
            if after_dt and applied and applied <= after_dt:
                continue
            if not S1BugReclaimService._audit_touches_normalized(audit, normalized, aliases):
                continue
            if appeal_id not in hits:
                hits.append(appeal_id)
        return sorted(hits)

    @staticmethod
    def _item_chain_group_key_from_candidate(candidate: dict) -> tuple:
        item_key = S1BugReclaimService._catalog_fuzzy_group_key(
            int(candidate.get('adventurer_id') or 0),
            candidate.get('item_name') or '',
            candidate.get('item_type') or '',
            candidate.get('item_rarity') or 'common',
        )
        return (*item_key, candidate.get('_appeal_source_key'))

    @staticmethod
    def _cluster_lineage_appeal_ids(candidate: dict) -> set[int]:
        """当前回收簇 lineage 涉及的申诉（不含后续改名/删堆等无关 audit）。"""
        ids: set[int] = set()
        for ev in candidate.get('lineage') or []:
            if ev.get('appeal_id'):
                ids.add(int(ev['appeal_id']))
            for key in ('seed_appeal_id', 'fork_appeal_id'):
                if ev.get(key):
                    ids.add(int(ev[key]))
        if candidate.get('appeal_id'):
            ids.add(int(candidate['appeal_id']))
        return ids

    @staticmethod
    def _fuzzy_group_lineage_appeal_map(candidates: list[dict]) -> dict[tuple, set[int]]:
        from collections import defaultdict

        groups: dict[tuple, set[int]] = defaultdict(set)
        for candidate in candidates:
            gkey = S1BugReclaimService._item_chain_group_key_from_candidate(candidate)
            groups[gkey].update(S1BugReclaimService._cluster_lineage_appeal_ids(candidate))
        return groups

    @staticmethod
    def _flow_verdict_label(verdict: str) -> str:
        return {
            'reappeal_same_adv': '同一冒险者多次申诉',
            'reappeal_downstream': '源头转出后买方申诉',
            'downstream_buyer_reappeal': '下游购后再申诉',
            'self_consumed': '申诉后已自用消耗',
            'transferred_no_appeal': '已转出且买方未申诉',
            'no_reappeal_chain': '无再申诉链路',
            'single_appeal_chain': '仅一次申诉',
            'create_item_chain': '申诉 create_item 链路',
            'shortfall_conflict': '与漏补冲突已跳过',
        }.get(verdict, verdict)

    @staticmethod
    def _chain_role_label(chain_role: str) -> str:
        return {
            'downstream_buyer': '下游买方（非源头）',
            'upstream_seller': '源头卖方',
            'source_reappeal': '源头再申诉',
        }.get(chain_role, chain_role)

    @staticmethod
    def _format_appeal_ids(appeal_ids: list | set | None) -> str:
        ids = sorted({int(a) for a in (appeal_ids or []) if a})
        if not ids:
            return ''
        if len(ids) == 1:
            return f'申诉 #{ids[0]}'
        return '申诉 ' + '、'.join(f'#{a}' for a in ids)

    @staticmethod
    def _build_reclaim_narrative(
        candidate: dict,
        market_prov: dict | None = None,
    ) -> dict:
        """预览 / 路由 / 执行日志共用的回收说明（面向玩家可见日志）。"""
        flow = candidate.get('post_appeal_flow') or {}
        verdict = str(flow.get('verdict') or '')
        chain_role = str(flow.get('chain_role') or '')
        item_name = str(candidate.get('item_name') or '物品')
        illegal_qty = int(candidate.get('illegal_quantity') or 0)
        appeal_ids = flow.get('appeal_ids') or []
        appeal_ref = S1BugReclaimService._format_appeal_ids(appeal_ids)
        legal_seed_qty = int(candidate.get('legal_seed_quantity') or 0)
        prior_seed = candidate.get('prior_legal_seed') or {}
        prov = market_prov or candidate.get('market_provenance')
        if not prov:
            prov = S1BugReclaimService._resolve_market_purchase_provenance(candidate)

        if verdict == 'downstream_buyer_reappeal' or chain_role == 'downstream_buyer':
            seller = (
                flow.get('upstream_seller_name')
                or (prov or {}).get('seller_name')
                or (prov or {}).get('seller_nickname')
                or (prov or {}).get('seller_username')
                or f"#{flow.get('upstream_seller_adventurer_id') or (prov or {}).get('seller_adventurer_id')}"
            )
            listing_id = flow.get('upstream_listing_id') or (prov or {}).get('listing_id')
            listing_note = f'挂单 #{listing_id}' if listing_id else '交易市场'
            summary = (
                f'你方非源头：该物品购自 {seller}（{listing_note}）。'
                f'卖方此前已通过数据申诉增加数量（源头首次归正视为合法），'
                f'经市场流转后买方不可再申诉改量；'
                f'{appeal_ref + " " if appeal_ref else ""}'
                f'产生的 +{illegal_qty} 全部无效，按扣物/扣金收回。'
            )
            item_desc = (
                f'申诉异常回收「{item_name}」×{{qty}}：购自已申诉补量源头 {seller}，'
                f'下游不可再改量，收回异常增量'
            )
            gold_desc = (
                f'申诉异常回收「{item_name}」扣金：购自已申诉补量源头 {seller}，'
                f'改量无效；存量不足部分按异常缺货价折算（认定 +{illegal_qty}）'
            )
        elif verdict == 'reappeal_downstream' and prov:
            seller = (
                prov.get('seller_name')
                or prov.get('seller_nickname')
                or prov.get('seller_username')
                or f"#{prov.get('seller_adventurer_id')}"
            )
            listing_id = prov.get('listing_id')
            listing_note = f'挂单 #{listing_id}' if listing_id else '交易市场'
            summary = (
                f'该物品购自 {seller}（{listing_note}）。'
                f'卖方此前已通过数据申诉增加该物数量，经市场流转后不可再次申诉改量；'
                f'{appeal_ref + " " if appeal_ref else ""}'
                f'你方后续产生的 +{illegal_qty} 全部认定无效，按扣物/扣金收回。'
            )
            item_desc = (
                f'申诉异常回收「{item_name}」×{{qty}}：购自 {seller} 的已申诉补量物品，'
                f'流转后不可再申诉改量，收回异常增量'
            )
            gold_desc = (
                f'申诉异常回收「{item_name}」扣金：购自 {seller} 的已申诉补量物品，'
                f'改量无效；存量不足部分按异常缺货价折算（认定 +{illegal_qty}）'
            )
        elif verdict == 'reappeal_downstream':
            downstream = flow.get('downstream_appeals') or []
            parts = []
            for d in downstream[:3]:
                buyer = d.get('buyer_name') or f"#{d.get('buyer_adventurer_id')}"
                bids = S1BugReclaimService._format_appeal_ids(d.get('buyer_appeal_ids'))
                parts.append(f'{buyer} {bids}'.strip())
            tail = '；'.join(parts) if parts else ''
            seed_note = ''
            seed_id = prior_seed.get('appeal_id')
            if seed_id or legal_seed_qty:
                seed_note = (
                    f'你方为源头，申诉 #{seed_id or "?"} 首次归正'
                    f'{" (+" + str(legal_seed_qty) + ")" if legal_seed_qty else ""} 视为合法；'
                )
            summary = (
                f'{seed_note}'
                f'物品转出后买方又申诉改量'
                f'{("：" + tail) if tail else ""}。'
                f'你方超出合法部分的 +{illegal_qty} 予以回收。'
            )
            item_desc = (
                f'申诉异常回收「{item_name}」×{{qty}}：'
                f'{"源头首次归正后转出，" if seed_id or legal_seed_qty else ""}'
                f'买方再申诉连带回收超额部分'
            )
            gold_desc = (
                f'申诉异常回收「{item_name}」扣金：'
                f'{"源头转出后买方再申诉，" if chain_role == "upstream_seller" else ""}'
                f'存量不足部分按异常缺货价折算（认定 +{illegal_qty}）'
            )
        elif verdict == 'reappeal_same_adv':
            seed = candidate.get('prior_legal_seed') or {}
            seed_id = seed.get('appeal_id')
            if seed_id:
                summary = (
                    f'申诉 #{seed_id} 已将数量归正为合法修正；'
                    f'{appeal_ref + " " if appeal_ref else ""}'
                    f'后续再次改量 +{illegal_qty} 超出首次合法部分，予以回收。'
                )
            else:
                summary = (
                    f'同一冒险者对「{item_name}」多次申诉改量；'
                    f'{appeal_ref + " " if appeal_ref else ""}'
                    f'超出规则允许的增量 +{illegal_qty} 予以回收。'
                )
            item_desc = (
                f'申诉异常回收「{item_name}」×{{qty}}：'
                f'{"首次归正后再次改量" if seed_id else "多次申诉改量"}，收回超额部分'
            )
            gold_desc = (
                f'申诉异常回收「{item_name}」扣金：'
                f'{"首次归正后再次改量" if seed_id else "多次申诉改量"}，'
                f'存量不足部分按异常缺货价折算（认定 +{illegal_qty}）'
            )
        elif verdict == 'create_item_chain':
            summary = (
                f'申诉链路含 create_item 新建补发；'
                f'{appeal_ref + " " if appeal_ref else ""}'
                f'同链路内后续改量/重复补发 +{illegal_qty} 认定非法并回收。'
            )
            item_desc = f'申诉异常回收「{item_name}」×{{qty}}：create_item 链路非法增量'
            gold_desc = (
                f'申诉异常回收「{item_name}」扣金：create_item 链路非法增量，'
                f'按异常缺货价折算（认定 +{illegal_qty}）'
            )
        else:
            detail = str(flow.get('detail') or '').strip()
            summary = detail or f'数据申诉历史漏洞产生的异常增量 +{illegal_qty}，予以回收。'
            item_desc = f'申诉异常回收「{item_name}」×{{qty}}'
            gold_desc = f'申诉异常回收「{item_name}」扣金（认定 +{illegal_qty}）'

        return {
            'summary': summary,
            'item_description': item_desc,
            'gold_description': gold_desc,
            'verdict': verdict,
            'chain_role': chain_role,
            'appeal_ref': appeal_ref,
        }

    @staticmethod
    def _item_reclaim_log_description(candidate: dict, quantity: int) -> str:
        narrative = candidate.get('reclaim_narrative') or S1BugReclaimService._build_reclaim_narrative(candidate)
        template = narrative.get('item_description') or '申诉异常回收「{name}」×{qty}'
        if '{qty}' in template:
            return template.format(qty=int(quantity))
        item_name = candidate.get('item_name') or '物品'
        return template.replace('{name}', str(item_name)) + f' ×{int(quantity)}'

    @staticmethod
    def _gold_reclaim_log_description(candidate: dict) -> str:
        narrative = candidate.get('reclaim_narrative') or S1BugReclaimService._build_reclaim_narrative(candidate)
        return str(narrative.get('gold_description') or f'申诉异常回收「{candidate.get("item_name")}」扣金')

    @staticmethod
    def _evaluate_post_appeal_flow(candidate: dict, rows) -> dict:
        """仅当申诉链继续（再申诉 / 买方再申诉）才应回收；自用或转出无申诉则跳过。"""
        lineage = candidate.get('lineage') or []
        appeal_ids = sorted({int(ev['appeal_id']) for ev in lineage if ev.get('appeal_id')})
        normalized = candidate['item_name']
        aliases = S1BugReclaimService._aliases(candidate)
        adventurer_id = int(candidate['adventurer_id'])
        since = candidate.get('_since')
        illegal_qty = int(candidate.get('illegal_quantity') or 0)
        first_appeal_id = appeal_ids[0] if appeal_ids else None

        prior_seed = candidate.get('prior_legal_seed') or {}
        if prior_seed:
            seed_id = prior_seed.get('appeal_id') or '?'
            return {
                'should_reclaim': True,
                'verdict': 'reappeal_same_adv',
                'chain_role': 'source_reappeal',
                'label': S1BugReclaimService._flow_verdict_label('reappeal_same_adv'),
                'detail': (
                    f'申诉 #{seed_id} 已作为首次数量归一合法种子；'
                    f'当前链路后续申诉额外增加 +{illegal_qty}，进入回收预览。'
                ),
                'appeal_ids': [int(seed_id)] + appeal_ids if str(seed_id).isdigit() else appeal_ids,
                'self_used_quantity': 0,
                'transferred_quantity': 0,
                'downstream_appeals': [],
            }

        create_steps = S1BugReclaimService._lineage_create_item_steps(lineage)
        issue_type = str(candidate.get('issue_type') or '')
        has_create_chain = (
            bool(create_steps)
            or issue_type in ('appeal_create_grant', 'duplicate_lineage_reappeal', 'mixed_lineage')
            or S1BugReclaimService._adventurer_has_create_item_audit(
                adventurer_id, normalized, aliases, rows,
            )
        )
        if illegal_qty > 0 and has_create_chain:
            create_count = len(create_steps) or sum(
                1 for ev in lineage if ev.get('event_type') == 'create_item'
            )
            detail_parts = []
            if create_count:
                detail_parts.append(f'链路含 {create_count} 步 create_item')
            if issue_type == 'appeal_qty_increase':
                detail_parts.append(f'create_item 后改量 +{illegal_qty}')
            elif create_count > 1 and len(appeal_ids) <= 1:
                detail_parts.append(f'同次申诉多次补发，合计 +{illegal_qty}')
            else:
                detail_parts.append(f'非法增量 +{illegal_qty}')
            if len(appeal_ids) > 1:
                detail_parts.append(
                    f'涉及申诉 {", ".join(f"#{a}" for a in appeal_ids)}'
                )
            return {
                'should_reclaim': True,
                'verdict': 'create_item_chain',
                'label': '申诉 create_item 链路',
                'detail': '；'.join(detail_parts),
                'appeal_ids': appeal_ids,
                'self_used_quantity': 0,
                'transferred_quantity': 0,
                'downstream_appeals': [],
            }

        if len(appeal_ids) >= 2:
            grant_note = '（含申诉 create_item）' if issue_type == 'appeal_create_grant' else ''
            return {
                'should_reclaim': True,
                'verdict': 'reappeal_same_adv',
                'label': S1BugReclaimService._flow_verdict_label('reappeal_same_adv'),
                'detail': (
                    f'同一冒险者 {len(appeal_ids)} 次申诉触及该物品{grant_note}：'
                    f'{", ".join(f"#{a}" for a in appeal_ids)}'
                ),
                'appeal_ids': appeal_ids,
                'self_used_quantity': 0,
                'transferred_quantity': 0,
                'downstream_appeals': [],
            }

        later_same_adv = S1BugReclaimService._later_appeal_ids_on_item(
            adventurer_id,
            normalized,
            aliases,
            since,
            rows,
            after_appeal_id=first_appeal_id,
        )
        if later_same_adv:
            grant_note = '申诉 create_item' if issue_type == 'appeal_create_grant' else '该物品'
            return {
                'should_reclaim': True,
                'verdict': 'reappeal_same_adv',
                'label': S1BugReclaimService._flow_verdict_label('reappeal_same_adv'),
                'detail': (
                    f'{grant_note}补发/改量后，同一冒险者再申诉：'
                    f'{", ".join(f"#{a}" for a in later_same_adv)}'
                ),
                'appeal_ids': appeal_ids + later_same_adv,
                'self_used_quantity': 0,
                'transferred_quantity': 0,
                'downstream_appeals': [],
            }

        self_used_qty = 0
        transferred: list[dict] = []
        logs_q = (
            db_manager.query(AdventurerS1ItemLog)
            .filter(
                AdventurerS1ItemLog.adventurer_id == adventurer_id,
                AdventurerS1ItemLog.item_name.in_(list(aliases)),
                AdventurerS1ItemLog.change_type.in_(('consume', 'overflow')),
            )
        )
        if since is not None:
            logs_q = logs_q.filter(AdventurerS1ItemLog.created_at >= since)
        logs = logs_q.order_by(AdventurerS1ItemLog.created_at.asc()).all()

        alias_norms = {S1BugReclaimService._normalize_name(a) for a in aliases}
        for log in logs:
            qty = max(1, int(log.quantity or 1))
            if log.source_type == 'market' and log.related_id and str(log.related_id).isdigit():
                listing = (
                    db_manager.query(AdventurerS1MarketListing)
                    .filter_by(id=int(log.related_id))
                    .first()
                )
                if listing and listing.status == 'completed' and listing.buyer_adventurer_id:
                    if S1BugReclaimService._normalize_name(listing.item_name) not in alias_norms:
                        self_used_qty += qty
                        continue
                    transferred.append({
                        'buyer_adventurer_id': int(listing.buyer_adventurer_id),
                        'quantity': qty,
                        'listing_id': int(listing.id),
                        'listing_type': listing.listing_type,
                        'completed_at': listing.completed_at or log.created_at,
                        'item_name': listing.item_name,
                    })
                    continue
                if listing and listing.status in ('active', 'cancelled'):
                    continue
            self_used_qty += qty

        downstream: list[dict] = []
        for tr in transferred:
            buyer_id = int(tr['buyer_adventurer_id'])
            buyer_appeals = S1BugReclaimService._later_appeal_ids_on_item(
                buyer_id,
                normalized,
                aliases,
                tr.get('completed_at'),
                rows,
                after_appeal_id=first_appeal_id,
            )
            if buyer_appeals:
                buyer = db_manager.query(AdventurerS1).filter_by(id=buyer_id).first()
                downstream.append({
                    **tr,
                    'buyer_adventurer_id': buyer_id,
                    'buyer_name': buyer.name if buyer else None,
                    'buyer_appeal_ids': buyer_appeals,
                })

        transferred_qty = sum(int(t.get('quantity') or 0) for t in transferred)

        if downstream:
            parts = [
                f'#{d["buyer_adventurer_id"]}{(" " + d["buyer_name"]) if d.get("buyer_name") else ""}'
                f' 申诉 {", ".join(f"#{a}" for a in d["buyer_appeal_ids"])}'
                for d in downstream
            ]
            grant_note = '申诉新补物品' if candidate.get('issue_type') == 'appeal_create_grant' else '物品'
            return {
                'should_reclaim': True,
                'verdict': 'reappeal_downstream',
                'chain_role': 'upstream_seller',
                'label': S1BugReclaimService._flow_verdict_label('reappeal_downstream'),
                'detail': f'{grant_note}转出后买方再申诉：' + '；'.join(parts),
                'appeal_ids': appeal_ids,
                'self_used_quantity': self_used_qty,
                'transferred_quantity': transferred_qty,
                'downstream_appeals': downstream,
            }

        if self_used_qty >= illegal_qty and illegal_qty > 0:
            return {
                'should_reclaim': False,
                'verdict': 'self_consumed',
                'label': S1BugReclaimService._flow_verdict_label('self_consumed'),
                'detail': f'申诉后 item_log 记录自用/消耗约 {self_used_qty}，覆盖异常增量 {illegal_qty}，不标记。',
                'appeal_ids': appeal_ids,
                'self_used_quantity': self_used_qty,
                'transferred_quantity': transferred_qty,
                'downstream_appeals': [],
            }

        if transferred_qty > 0:
            return {
                'should_reclaim': False,
                'verdict': 'transferred_no_appeal',
                'label': S1BugReclaimService._flow_verdict_label('transferred_no_appeal'),
                'detail': f'已转出约 {transferred_qty}，买方未再申诉，不标记。',
                'appeal_ids': appeal_ids,
                'self_used_quantity': self_used_qty,
                'transferred_quantity': transferred_qty,
                'downstream_appeals': [],
            }

        return {
            'should_reclaim': False,
            'verdict': 'no_reappeal_chain',
            'label': S1BugReclaimService._flow_verdict_label('no_reappeal_chain'),
            'detail': '仅一次申诉改量，未发现再申诉或买方申诉链路，不标记。',
            'appeal_ids': appeal_ids,
            'self_used_quantity': self_used_qty,
            'transferred_quantity': transferred_qty,
            'downstream_appeals': [],
        }

    @staticmethod
    def _find_market_purchase_current_stack(listing: AdventurerS1MarketListing):
        buyer_id = int(listing.buyer_adventurer_id or 0)
        if buyer_id <= 0:
            return None
        rows = (
            db_manager.query(AdventurerS1WarehouseItem)
            .filter(
                AdventurerS1WarehouseItem.adventurer_id == buyer_id,
                AdventurerS1WarehouseItem.name == listing.item_name,
                AdventurerS1WarehouseItem.type == (listing.item_type or ''),
                AdventurerS1WarehouseItem.rarity == (listing.rarity or 'common'),
            )
            .order_by(AdventurerS1WarehouseItem.id.desc())
            .all()
        )
        if not rows:
            return None
        desc = str(listing.custom_desc or '').strip()
        for row in rows:
            if str(row.custom_desc or '').strip() == desc and row.uses_left == listing.uses_left:
                return row
        return rows[0]

    @staticmethod
    def _build_market_downstream_candidate(candidate: dict, transfer: dict, excluded: set[str]) -> dict | None:
        if not transfer.get('buyer_appeal_ids'):
            return None
        listing_id = transfer.get('listing_id')
        if not listing_id:
            return None
        listing = (
            db_manager.query(AdventurerS1MarketListing)
            .filter_by(id=int(listing_id))
            .first()
        )
        if not listing or listing.status != 'completed' or not listing.buyer_adventurer_id:
            return None
        if listing.listing_type != 'sell':
            return None
        stack = S1BugReclaimService._find_market_purchase_current_stack(listing)
        if not stack:
            return None

        buyer_id = int(listing.buyer_adventurer_id)
        issue_key = f'market_downstream:{candidate.get("issue_key")}:listing:{int(listing.id)}:buyer:{buyer_id}'
        if issue_key in excluded:
            return None

        buyer = db_manager.query(AdventurerS1).filter_by(id=buyer_id).first()
        aliases = set(candidate.get('_name_aliases') or [])
        aliases.add(listing.item_name)
        aliases.add(S1BugReclaimService._normalize_name(listing.item_name))
        qty = min(
            max(1, int(transfer.get('quantity') or listing.quantity or 1)),
            max(1, int(candidate.get('illegal_quantity') or 1)),
        )
        buyer_appeal_ids = [int(a) for a in transfer.get('buyer_appeal_ids') or [] if a]
        return {
            'issue_key': issue_key,
            'issue_type': 'market_downstream_purchase',
            'appeal_id': candidate.get('appeal_id'),
            'audit_log_id': candidate.get('audit_log_id'),
            'adventurer_id': buyer_id,
            'user_id': int(buyer.owner_id) if buyer and buyer.owner_id else None,
            'item_name': listing.item_name,
            'item_type': listing.item_type or '',
            'item_rarity': listing.rarity or 'common',
            'illegal_quantity': qty,
            'target_type': 'warehouse',
            'target_id': int(stack.id),
            'original_name': candidate.get('original_name'),
            'original_desc': candidate.get('original_desc'),
            'current_name': stack.name,
            'current_desc': stack.custom_desc,
            'lineage': candidate.get('lineage') or [],
            'lineage_count': int(candidate.get('lineage_count') or len(candidate.get('lineage') or [])),
            'appeal_timeline': candidate.get('appeal_timeline') or {},
            'before': candidate.get('before') or {},
            'after': {
                **(candidate.get('after') or {}),
                'market_downstream': {
                    'source_issue_key': candidate.get('issue_key'),
                    'listing_id': int(listing.id),
                    'seller_adventurer_id': int(listing.seller_adventurer_id),
                    'buyer_adventurer_id': buyer_id,
                    'quantity': qty,
                },
            },
            'created_at': to_utc_iso(listing.completed_at) if listing.completed_at else candidate.get('created_at'),
            'applied_at': candidate.get('applied_at'),
            '_since': listing.completed_at or transfer.get('completed_at') or candidate.get('_since'),
            '_name_aliases': aliases,
            'post_appeal_flow': {
                'should_reclaim': True,
                'verdict': 'reappeal_downstream',
                'label': S1BugReclaimService._flow_verdict_label('reappeal_downstream'),
                'detail': '申诉异常增量已通过交易市场卖给买方，且买方后续申诉命中该物品；买方现存市场购入堆进入回收预览。',
                'appeal_ids': sorted(set(
                    ((candidate.get('post_appeal_flow') or {}).get('appeal_ids') or [])
                    + buyer_appeal_ids
                )),
                'self_used_quantity': 0,
                'transferred_quantity': qty,
                'downstream_appeals': [transfer],
            },
        }

    @staticmethod
    def _apply_post_appeal_flow_filter(candidates: list[dict], rows) -> tuple[list[dict], dict]:
        group_appeals = S1BugReclaimService._fuzzy_group_lineage_appeal_map(candidates)
        eligible: list[dict] = []
        skipped: dict[str, int] = {}
        excluded = S1BugReclaimService._executed_issue_keys()
        executed_shortfalls = None
        for candidate in candidates:
            flow = S1BugReclaimService._evaluate_post_appeal_flow(candidate, rows)
            downstream_flow = S1BugReclaimService._market_downstream_reappeal_flow(candidate)
            if downstream_flow:
                flow = downstream_flow
            gkey = S1BugReclaimService._item_chain_group_key_from_candidate(candidate)
            lineage_group = group_appeals.get(gkey) or set()
            if len(lineage_group) <= 1 and flow.get('verdict') not in (
                'reappeal_downstream', 'downstream_buyer_reappeal',
            ):
                only_id = min(lineage_group) if lineage_group else None
                flow = {
                    **flow,
                    'should_reclaim': False,
                    'verdict': 'single_appeal_chain',
                    'label': S1BugReclaimService._flow_verdict_label('single_appeal_chain'),
                    'detail': (
                        f'该物品回收链路仅涉及 1 次申诉'
                        f'{f"（#{only_id}）" if only_id else ""}，不进入回收预览。'
                    ),
                    'appeal_ids': sorted(lineage_group),
                    'self_used_quantity': flow.get('self_used_quantity', 0),
                    'transferred_quantity': flow.get('transferred_quantity', 0),
                    'downstream_appeals': flow.get('downstream_appeals') or [],
                }
            candidate['post_appeal_flow'] = flow
            if flow.get('should_reclaim'):
                if executed_shortfalls is None:
                    executed_shortfalls = S1BugAppealQtyGuard.executed_shortfall_by_appeal_slot()
                block_reason = S1BugAppealQtyGuard.reclaim_blocked_reason(
                    adventurer_id=int(candidate.get('adventurer_id') or 0),
                    appeal_id=candidate.get('appeal_id'),
                    target_type=candidate.get('target_type'),
                    target_id=candidate.get('target_id'),
                    illegal_quantity=int(candidate.get('illegal_quantity') or 0),
                    executed_shortfalls=executed_shortfalls,
                )
                if block_reason:
                    flow = {
                        **flow,
                        'should_reclaim': False,
                        'verdict': 'shortfall_conflict',
                        'label': '与漏补冲突已跳过',
                        'detail': block_reason,
                    }
                    candidate['post_appeal_flow'] = flow
                    skipped['shortfall_conflict'] = skipped.get('shortfall_conflict', 0) + 1
                    continue
                eligible.append(candidate)
                if flow.get('verdict') in ('reappeal_downstream', 'downstream_buyer_reappeal'):
                    for transfer in flow.get('downstream_appeals') or []:
                        downstream = S1BugReclaimService._build_market_downstream_candidate(
                            candidate, transfer, excluded,
                        )
                        if downstream and flow.get('verdict') == 'reappeal_downstream':
                            eligible.append(downstream)
            else:
                verdict = str(flow.get('verdict') or 'unknown')
                skipped[verdict] = skipped.get(verdict, 0) + 1
        return eligible, skipped

    @staticmethod
    def _trace_route(candidate: dict) -> dict:
        market_prov = S1BugReclaimService._resolve_market_purchase_provenance(candidate)
        narrative = S1BugReclaimService._build_reclaim_narrative(candidate, market_prov)
        unit_price = S1BuybackService.compute_recycle_price(
            candidate['item_name'], candidate.get('item_type') or '', candidate.get('item_rarity') or 'common',
        )
        missing_item_unit_price = S1BugReclaimService._missing_item_unit_price(
            unit_price, candidate.get('item_rarity') or 'common',
        )
        aliases = S1BugReclaimService._aliases(candidate)
        since = candidate.get('_since')
        remaining = int(candidate['illegal_quantity'])
        route = []

        current_qty = 0
        current_breakdown = []
        for source, row in S1BugReclaimService._current_items(candidate, all_matching=True):
            qty = item_quantity_of(row)
            current_qty += qty
            current_breakdown.append({'source': source, 'id': row.id, 'name': row.name, 'quantity': qty})
        reclaim_item_qty = min(remaining, current_qty)
        if reclaim_item_qty > 0:
            route.append({
                'kind': 'current_item',
                'confidence': 'inferred',
                'quantity': reclaim_item_qty,
                'gold': 0,
                'details': current_breakdown,
                'message': narrative['summary'],
            })
            remaining -= reclaim_item_qty

        if remaining > 0:
            logs_q = db_manager.query(AdventurerS1ItemLog).filter(
                AdventurerS1ItemLog.adventurer_id == candidate['adventurer_id'],
                AdventurerS1ItemLog.item_name.in_(aliases),
                AdventurerS1ItemLog.change_type != 'gain',
            )
            if since is not None:
                logs_q = logs_q.filter(AdventurerS1ItemLog.created_at >= since)
            logs = logs_q.order_by(AdventurerS1ItemLog.created_at.asc()).limit(100).all()
            for log in logs:
                if remaining <= 0:
                    break
                qty = min(remaining, int(log.quantity or 1))
                gold = missing_item_unit_price * qty
                kind = log.source_type or 'unknown'
                confidence = 'inferred'
                details = {'item_log_id': log.id, 'description': log.description}
                if log.source_type == 'market' and log.related_id:
                    listing = AdventurerS1MarketListing.query.session.get(AdventurerS1MarketListing, int(log.related_id)) if str(log.related_id).isdigit() else None
                    if listing and listing.status == 'completed':
                        confidence = 'exact'
                        details['listing_id'] = listing.id
                        details['listing_type'] = listing.listing_type
                        if listing.listing_type == 'sell':
                            income = S1BugReclaimService._market_sell_income(candidate['adventurer_id'], listing.id)
                            if income > 0:
                                listing_qty = max(1, item_quantity_of(listing))
                                gold = round(income * (qty / listing_qty), 2)
                            kind = 'market_sell'
                        else:
                            kind = 'market_barter'
                    elif listing and listing.status in ('active', 'cancelled'):
                        continue
                route.append({
                    'kind': kind,
                    'confidence': confidence,
                    'quantity': qty,
                    'gold': round(float(gold or 0), 2),
                    'details': details,
                    'message': S1BugReclaimService._route_message(kind),
                })
                remaining -= qty

        if remaining > 0:
            buyback_q = db_manager.query(AdventurerS1CurrencyLog).filter(
                AdventurerS1CurrencyLog.adventurer_id == candidate['adventurer_id'],
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == 'buyback_sell',
                AdventurerS1CurrencyLog.change_amount > 0,
            )
            if since is not None:
                buyback_q = buyback_q.filter(AdventurerS1CurrencyLog.created_at >= since)
            buyback_logs = buyback_q.order_by(AdventurerS1CurrencyLog.created_at.asc()).limit(100).all()
            for log in buyback_logs:
                if remaining <= 0:
                    break
                desc = str(log.description or '')
                if not any(alias and alias in desc for alias in aliases):
                    continue
                inferred_qty = max(1, int(round(float(log.change_amount or 0) / max(unit_price, 1))))
                qty = min(remaining, inferred_qty)
                gold = min(round(float(log.change_amount or 0), 2), round(float(unit_price * qty), 2))
                route.append({
                    'kind': 'buyback_sell',
                    'confidence': 'inferred',
                    'quantity': qty,
                    'gold': gold,
                    'details': {'currency_log_id': log.id, 'description': log.description},
                    'message': S1BugReclaimService._route_message('buyback_sell'),
                })
                remaining -= qty

        if remaining > 0:
            route.append({
                'kind': 'unknown_consumed',
                'confidence': 'partial',
                'quantity': remaining,
                'gold': round(float(missing_item_unit_price * remaining), 2),
                'details': {},
                'message': (
                    f'现存物品不足，剩余 {remaining} 件按异常缺货价扣金币。'
                    f' {narrative["summary"]}'
                ).strip(),
            })

        gold_total = round(sum(float(r.get('gold') or 0) for r in route), 2)
        return {
            'reclaim_mode': 'default',
            'reclaim_narrative': narrative,
            'market_provenance': market_prov,
            'unit_recycle_price': unit_price,
            'unit_missing_item_price': missing_item_unit_price,
            'route': route,
            'current_quantity': current_qty,
            'reclaim_item_quantity': reclaim_item_qty,
            'gold_to_deduct': gold_total,
            'buyer_refund_gold': 0.0,
            'seller_clawback_gold': 0.0,
        }

    @staticmethod
    def _market_sell_income(adventurer_id: int, listing_id: int) -> float:
        rows = db_manager.query(AdventurerS1CurrencyLog).filter_by(
            adventurer_id=adventurer_id,
            currency_code='gold',
            source_type='market_sell',
            related_id=str(listing_id),
            related_type='market_listing',
        ).all()
        gold_income = sum(
            float(row.change_amount or 0)
            for row in rows
            if float(row.change_amount or 0) > 0
        )
        temporary_income = S1BugReclaimService._market_sell_temporary_income(
            adventurer_id,
            listing_id,
        )
        return round(gold_income + temporary_income, 2)

    @staticmethod
    def _route_message(kind: str) -> str:
        return {
            'market_sell': '交易市场卖钱，扣实际到账金币。',
            'market_barter': '交易市场以物易物，按异常缺货价扣金币。',
            'building': '建筑或治疗消耗，按异常缺货价扣金币。',
            'buyback_sell': '收购商行出售，扣实际/折算金币。',
        }.get(kind, '物品已流出，按异常缺货价扣金币。')

    @staticmethod
    def _candidate_to_json(candidate: dict) -> dict:
        def json_safe(value):
            if isinstance(value, dict):
                return {key: json_safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_safe(item) for item in value]
            if isinstance(value, set):
                return [json_safe(item) for item in sorted(value)]
            if isinstance(value, datetime):
                return to_utc_iso(value)
            return value

        return json_safe(candidate)

    @staticmethod
    def _candidate_from_json(payload: dict) -> dict:
        candidate = dict(payload or {})
        aliases = candidate.get('_name_aliases')
        if isinstance(aliases, list):
            candidate['_name_aliases'] = set(str(v) for v in aliases if v)
        since = candidate.get('_since')
        if since and not isinstance(since, datetime):
            from utils.time_utils import parse_iso_date
            candidate['_since'] = parse_iso_date(str(since))
        return candidate

    @staticmethod
    def _lock_incremental_scan_transaction() -> None:
        """串行化扫描游标与候选池变更；调用方必须已开启事务。"""
        db_uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI') or '')
        if 'postgresql' not in db_uri:
            return
        from extensions import db
        db.session.execute(
            text('SELECT pg_advisory_xact_lock(:key)'),
            {'key': _INCREMENTAL_SCAN_ADVISORY_LOCK_KEY},
        )

    @staticmethod
    def mark_adventurers_dirty(adventurer_ids: set[int]) -> None:
        """标记历史审计状态已变化，下一次扫描精确重算这些冒险者。"""
        normalized_ids = {
            int(adventurer_id)
            for adventurer_id in (adventurer_ids or set())
            if int(adventurer_id) > 0
        }
        if not normalized_ids:
            return

        S1BugReclaimService._lock_incremental_scan_transaction()
        state = (
            db_manager.query(AdventurerS1BugReclaimScanState)
            .filter_by(id=1)
            .with_for_update()
            .first()
        )
        if state is None or state.detection_mode != _DETECTION_MODE:
            return
        dirty_ids = {
            int(adventurer_id)
            for adventurer_id in (state.dirty_adventurer_ids_json or [])
            if int(adventurer_id) > 0
        }
        dirty_ids.update(normalized_ids)
        state.dirty_adventurer_ids_json = sorted(dirty_ids)
        state.dirty_revision = int(state.dirty_revision or 0) + 1

    @staticmethod
    def _scan_batch_size() -> int:
        return max(1, min(int(current_app.config.get(
            'ADVENTURER_S1_BUG_RECLAIM_SCAN_ADVENTURER_BATCH_SIZE', 20,
        ) or 20), 100))

    @staticmethod
    def _scan_audit_row_budget() -> int:
        return max(1, min(int(current_app.config.get(
            'ADVENTURER_S1_BUG_RECLAIM_SCAN_AUDIT_ROW_BUDGET', 200,
        ) or 200), 5000))

    @staticmethod
    def _bound_scan_source_batch(
        source_ids: list[int],
        *,
        through_audit_id: int,
    ) -> list[int]:
        """轻历史来源合批；重历史来源至少单独推进一个，避免饥饿。"""
        normalized_ids = list(dict.fromkeys(
            int(adventurer_id)
            for adventurer_id in (source_ids or [])
            if int(adventurer_id) > 0
        ))
        if len(normalized_ids) <= 1:
            return normalized_ids

        count_rows = (
            S1BugReclaimService._approved_audit_query()
            .filter(
                AdventurerS1DataAppealAuditLog.id <= int(through_audit_id),
                AdventurerS1DataAppealAuditLog.adventurer_id.in_(normalized_ids),
            )
            .with_entities(
                AdventurerS1DataAppealAuditLog.adventurer_id,
                func.count(AdventurerS1DataAppealAuditLog.id),
            )
            .group_by(AdventurerS1DataAppealAuditLog.adventurer_id)
            .all()
        )
        row_count_by_adventurer = {
            int(row[0]): int(row[1] or 0)
            for row in count_rows
            if row and int(row[0] or 0) > 0
        }
        budget = S1BugReclaimService._scan_audit_row_budget()
        selected: list[int] = []
        selected_rows = 0
        for adventurer_id in normalized_ids:
            row_count = row_count_by_adventurer.get(adventurer_id, 0)
            if selected and selected_rows + row_count > budget:
                break
            selected.append(adventurer_id)
            selected_rows += row_count
        return selected

    @staticmethod
    def _scan_claim_ttl_seconds() -> int:
        return max(30, min(int(current_app.config.get(
            'ADVENTURER_S1_BUG_RECLAIM_SCAN_CLAIM_TTL_SECONDS', 300,
        ) or 300), 3600))

    @staticmethod
    def _pending_candidate_count() -> int:
        return int(db_manager.query(AdventurerS1BugReclaimCandidate).filter(
            AdventurerS1BugReclaimCandidate.detection_mode == _DETECTION_MODE,
        ).count())

    @staticmethod
    def _candidate_source_adventurer_id(candidate: dict) -> int:
        downstream = (candidate.get('after') or {}).get('market_downstream') or {}
        source_id = downstream.get('seller_adventurer_id')
        if source_id is None:
            source_id = candidate.get('adventurer_id')
        return int(source_id or 0)

    @staticmethod
    def _dependent_reclaim_source_ids(
        adventurer_ids: set[int],
        *,
        include_market_sellers: bool,
    ) -> set[int]:
        """找到显示在本批玩家名下、但必须由上游源链重算的冒险者。"""
        if not adventurer_ids:
            return set()
        dependent_ids = {
            int(row[0])
            for row in (
                db_manager.query(AdventurerS1BugReclaimCandidate.source_adventurer_id)
                .filter(
                    AdventurerS1BugReclaimCandidate.detection_mode == _DETECTION_MODE,
                    AdventurerS1BugReclaimCandidate.adventurer_id.in_(adventurer_ids),
                    AdventurerS1BugReclaimCandidate.source_adventurer_id
                    != AdventurerS1BugReclaimCandidate.adventurer_id,
                )
                .distinct()
                .all()
            )
            if row and int(row[0] or 0) > 0
        }
        if include_market_sellers:
            dependent_ids.update({
                int(row[0])
                for row in (
                    db_manager.query(
                        AdventurerS1BugReclaimCandidate.source_adventurer_id,
                    )
                    .join(
                        AdventurerS1MarketListing,
                        AdventurerS1MarketListing.seller_adventurer_id
                        == AdventurerS1BugReclaimCandidate.source_adventurer_id,
                    )
                    .filter(
                        AdventurerS1BugReclaimCandidate.detection_mode
                        == _DETECTION_MODE,
                        AdventurerS1MarketListing.buyer_adventurer_id.in_(adventurer_ids),
                        AdventurerS1MarketListing.status == 'completed',
                    )
                    .distinct()
                    .all()
                )
                if row and int(row[0] or 0) > 0
            })
        return dependent_ids - adventurer_ids

    @staticmethod
    def _scan_progress_meta(
        state: AdventurerS1BugReclaimScanState,
        *,
        complete: bool,
        busy: bool = False,
        processed_this_batch: int = 0,
        audit_rows: int = 0,
        new_audit_rows: int = 0,
        refreshed_candidates: int = 0,
        dirty_adventurers: int | None = None,
        skipped_by_flow: dict | None = None,
        cluster_meta: dict | None = None,
        mode_override: str | None = None,
    ) -> dict:
        pending_count = S1BugReclaimService._pending_candidate_count()
        mode = mode_override or str(state.scan_mode or 'incremental')
        base_audit_id = int(state.scan_base_audit_log_id or 0)
        target_audit_id = int(state.scan_target_audit_log_id or 0)
        return {
            **(cluster_meta or {}),
            'complete': bool(complete),
            'status': 'ready' if complete else 'scanning',
            'mode': mode,
            'busy': bool(busy),
            'cursor_before': base_audit_id,
            'cursor_after': target_audit_id if complete else base_audit_id,
            'last_audit_log_id': int(state.last_audit_log_id or 0),
            'target_audit_log_id': target_audit_id,
            'scan_cursor_adventurer_id': int(state.scan_cursor_adventurer_id or 0),
            'processed_adventurers': int(state.scan_processed_adventurers or 0),
            'total_adventurers': int(state.scan_total_adventurers or 0),
            'processed_this_batch': int(processed_this_batch),
            'new_audit_rows': int(new_audit_rows),
            'audit_rows': int(audit_rows),
            'affected_adventurers': int(processed_this_batch),
            'dirty_adventurers': (
                len(state.dirty_adventurer_ids_json or [])
                if dirty_adventurers is None else int(dirty_adventurers)
            ),
            'pending_candidates': pending_count,
            'refreshed_candidates': int(refreshed_candidates),
            'skipped_by_flow': skipped_by_flow or {},
            'raw_cluster_count': pending_count,
            'retry_after_ms': 0 if complete else (500 if busy else 100),
        }

    @staticmethod
    def _claim_scan_batch() -> tuple[dict | None, dict | None]:
        """短事务内启动/续接扫描并领取一个源冒险者批次。"""
        now = _portable_utc_now()
        batch_size = S1BugReclaimService._scan_batch_size()
        with db_manager.transaction():
            S1BugReclaimService._lock_incremental_scan_transaction()
            state = (
                db_manager.query(AdventurerS1BugReclaimScanState)
                .filter_by(id=1)
                .with_for_update()
                .first()
            )
            full_rebuild = state is None or state.detection_mode != _DETECTION_MODE
            if state is None:
                state = AdventurerS1BugReclaimScanState(
                    id=1,
                    detection_mode=_DETECTION_MODE,
                    last_audit_log_id=0,
                    dirty_adventurer_ids_json=[],
                )
                db_manager.add(state)

            max_audit_id = S1BugReclaimService._max_approved_audit_id()
            dirty_ids = {
                int(adventurer_id)
                for adventurer_id in (state.dirty_adventurer_ids_json or [])
                if int(adventurer_id) > 0
            }

            if full_rebuild:
                db_manager.query(AdventurerS1BugReclaimCandidate).delete(
                    synchronize_session=False,
                )
                state.detection_mode = _DETECTION_MODE
                state.last_audit_log_id = 0
                state.dirty_adventurer_ids_json = []
                state.dirty_revision = 0
                state.scan_status = 'scanning'
                state.scan_mode = 'full_baseline'
                state.scan_base_audit_log_id = 0
                state.scan_target_audit_log_id = max_audit_id
                state.scan_cursor_adventurer_id = 0
                state.scan_total_adventurers = (
                    S1BugReclaimService._scan_source_adventurer_count(
                        after_audit_id=0,
                        through_audit_id=max_audit_id,
                    )
                )
                state.scan_processed_adventurers = 0
                state.scan_claim_token = None
                state.scan_claimed_at = None
                state.scan_last_error = None
                dirty_ids = set()
            elif state.scan_status != 'scanning':
                cursor_before = int(state.last_audit_log_id or 0)
                if max_audit_id <= cursor_before and not dirty_ids:
                    state.scan_base_audit_log_id = cursor_before
                    state.scan_target_audit_log_id = cursor_before
                    state.scan_cursor_adventurer_id = 0
                    state.scan_total_adventurers = 0
                    state.scan_processed_adventurers = 0
                    return None, S1BugReclaimService._scan_progress_meta(
                        state,
                        complete=True,
                        mode_override='incremental_noop',
                    )

                source_count = S1BugReclaimService._scan_source_adventurer_count(
                    after_audit_id=cursor_before,
                    through_audit_id=max_audit_id,
                )
                dirty_overlap = 0
                if dirty_ids and max_audit_id > cursor_before:
                    dirty_overlap = int(
                        S1BugReclaimService._scan_source_adventurer_query(
                            after_audit_id=cursor_before,
                            through_audit_id=max_audit_id,
                        )
                        .filter(
                            AdventurerS1DataAppealAuditLog.adventurer_id.in_(dirty_ids)
                        )
                        .with_entities(func.count(func.distinct(
                            AdventurerS1DataAppealAuditLog.adventurer_id,
                        )))
                        .scalar()
                        or 0
                    )
                state.scan_status = 'scanning'
                state.scan_mode = 'incremental'
                state.scan_base_audit_log_id = cursor_before
                state.scan_target_audit_log_id = max_audit_id
                state.scan_cursor_adventurer_id = 0
                state.scan_total_adventurers = (
                    source_count + max(0, len(dirty_ids) - dirty_overlap)
                )
                state.scan_processed_adventurers = 0
                state.scan_claim_token = None
                state.scan_claimed_at = None
                state.scan_last_error = None

            claimed_at = state.scan_claimed_at
            if claimed_at is not None:
                claimed_at = claimed_at.replace(tzinfo=None)
            claim_is_fresh = bool(
                state.scan_claim_token
                and claimed_at is not None
                and now - claimed_at < timedelta(
                    seconds=S1BugReclaimService._scan_claim_ttl_seconds(),
                )
            )
            if claim_is_fresh:
                return None, S1BugReclaimService._scan_progress_meta(
                    state, complete=False, busy=True,
                )

            base_audit_id = int(state.scan_base_audit_log_id or 0)
            target_audit_id = int(state.scan_target_audit_log_id or 0)
            cursor_before = int(state.scan_cursor_adventurer_id or 0)
            source_ids = S1BugReclaimService._next_scan_source_adventurer_ids(
                after_audit_id=base_audit_id,
                through_audit_id=target_audit_id,
                after_adventurer_id=cursor_before,
                limit=batch_size,
            )
            source_ids = S1BugReclaimService._bound_scan_source_batch(
                source_ids,
                through_audit_id=target_audit_id,
            )
            batch_kind = 'audit_sources'
            if not source_ids:
                dirty_ids = {
                    int(adventurer_id)
                    for adventurer_id in (state.dirty_adventurer_ids_json or [])
                    if int(adventurer_id) > 0
                }
                source_ids = sorted(dirty_ids)[:batch_size]
                source_ids = S1BugReclaimService._bound_scan_source_batch(
                    source_ids,
                    through_audit_id=target_audit_id,
                )
                batch_kind = 'dirty'

            if source_ids:
                source_id_set = set(source_ids)
                dependency_ids = S1BugReclaimService._dependent_reclaim_source_ids(
                    source_id_set,
                    include_market_sellers=(
                        state.scan_mode == 'incremental'
                        and batch_kind == 'audit_sources'
                    ),
                )
                if dependency_ids:
                    already_scheduled_rows = (
                        S1BugReclaimService._scan_source_adventurer_query(
                            after_audit_id=base_audit_id,
                            through_audit_id=target_audit_id,
                        )
                        .filter(
                            AdventurerS1DataAppealAuditLog.adventurer_id.in_(dependency_ids),
                            AdventurerS1DataAppealAuditLog.adventurer_id > cursor_before,
                        )
                        .distinct()
                        .all()
                    )
                    already_scheduled = {
                        int(row[0]) for row in already_scheduled_rows if row
                    }
                    dependency_ids -= already_scheduled
                current_dirty_ids = {
                    int(adventurer_id)
                    for adventurer_id in (state.dirty_adventurer_ids_json or [])
                    if int(adventurer_id) > 0
                }
                new_dependency_ids = dependency_ids - current_dirty_ids
                if new_dependency_ids:
                    current_dirty_ids.update(new_dependency_ids)
                    state.dirty_adventurer_ids_json = sorted(current_dirty_ids)
                    state.dirty_revision = int(state.dirty_revision or 0) + 1
                    state.scan_total_adventurers = (
                        int(state.scan_total_adventurers or 0)
                        + len(new_dependency_ids)
                    )

            if not source_ids:
                state.scan_status = 'ready'
                state.last_audit_log_id = target_audit_id
                final_progress = max(
                    int(state.scan_total_adventurers or 0),
                    int(state.scan_processed_adventurers or 0),
                )
                state.scan_total_adventurers = final_progress
                state.scan_processed_adventurers = final_progress
                state.scan_claim_token = None
                state.scan_claimed_at = None
                state.scan_last_error = None
                return None, S1BugReclaimService._scan_progress_meta(
                    state, complete=True,
                )

            claim_token = uuid4().hex
            state.scan_claim_token = claim_token
            state.scan_claimed_at = now
            state.scan_last_error = None
            return {
                'claim_token': claim_token,
                'scan_mode': str(state.scan_mode or 'incremental'),
                'batch_kind': batch_kind,
                'source_adventurer_ids': source_ids,
                'base_audit_log_id': base_audit_id,
                'target_audit_log_id': target_audit_id,
                'cursor_before': cursor_before,
                'dirty_revision': int(state.dirty_revision or 0),
            }, None

    @staticmethod
    def _read_scan_batch(spec: dict) -> tuple[list[tuple], set[str]]:
        """短读事务中只冻结已领取源历史；无候选时不读取市场买方。"""
        source_ids = set(spec['source_adventurer_ids'])
        with db_manager.transaction():
            history_rows = S1BugReclaimService._fetch_approved_audit_rows(
                through_id=spec['target_audit_log_id'],
                adventurer_ids=source_ids,
            )
            excluded = S1BugReclaimService._executed_issue_keys()
        return history_rows, excluded

    @staticmethod
    def _read_scan_flow_rows(
        spec: dict,
        history_rows: list[tuple],
        candidates: list[dict],
    ) -> list[tuple]:
        """仅为确有候选的源补齐可能承接市场链的买方申诉历史。"""
        claimed_source_ids = set(spec['source_adventurer_ids'])
        candidate_source_ids = {
            int(candidate.get('adventurer_id') or 0)
            for candidate in (candidates or [])
            if int(candidate.get('adventurer_id') or 0) in claimed_source_ids
        }
        if not candidate_source_ids:
            return history_rows

        with db_manager.transaction():
            buyer_rows = (
                db_manager.query(AdventurerS1MarketListing.buyer_adventurer_id)
                .filter(
                    AdventurerS1MarketListing.seller_adventurer_id.in_(
                        candidate_source_ids,
                    ),
                    AdventurerS1MarketListing.status == 'completed',
                    AdventurerS1MarketListing.buyer_adventurer_id.isnot(None),
                )
                .distinct()
                .all()
            )
            buyer_ids = {
                int(row[0]) for row in buyer_rows
                if row
                and int(row[0] or 0) > 0
                and int(row[0]) not in claimed_source_ids
            }
            downstream_rows = S1BugReclaimService._fetch_approved_audit_rows(
                through_id=spec['target_audit_log_id'],
                adventurer_ids=buyer_ids,
                include_source_snapshots=False,
            )
        return history_rows + downstream_rows

    @staticmethod
    def _release_failed_scan_claim(claim_token: str, error: Exception) -> None:
        with db_manager.transaction():
            S1BugReclaimService._lock_incremental_scan_transaction()
            state = (
                db_manager.query(AdventurerS1BugReclaimScanState)
                .filter_by(id=1)
                .with_for_update()
                .first()
            )
            if state is None or state.scan_claim_token != claim_token:
                return
            state.scan_claim_token = None
            state.scan_claimed_at = None
            state.scan_last_error = str(error)[:1000]

    @staticmethod
    def _persist_scan_batch(
        spec: dict,
        pending_rows: list[AdventurerS1BugReclaimCandidate],
        *,
        history_rows: list[tuple],
        cluster_meta: dict,
        flow_skipped: dict,
    ) -> dict | None:
        with db_manager.transaction():
            S1BugReclaimService._lock_incremental_scan_transaction()
            state = (
                db_manager.query(AdventurerS1BugReclaimScanState)
                .filter_by(id=1)
                .with_for_update()
                .first()
            )
            if (
                state is None
                or state.detection_mode != _DETECTION_MODE
                or state.scan_status != 'scanning'
                or state.scan_claim_token != spec['claim_token']
                or str(state.scan_mode or '') != spec['scan_mode']
                or int(state.scan_base_audit_log_id or 0) != spec['base_audit_log_id']
                or int(state.scan_target_audit_log_id or 0) != spec['target_audit_log_id']
                or int(state.scan_cursor_adventurer_id or 0) != spec['cursor_before']
            ):
                return None

            source_ids = set(spec['source_adventurer_ids'])
            current_excluded = S1BugReclaimService._executed_issue_keys()
            rows_to_store = [
                row for row in pending_rows if row.issue_key not in current_excluded
            ]
            (
                db_manager.query(AdventurerS1BugReclaimCandidate)
                .filter(
                    AdventurerS1BugReclaimCandidate.source_adventurer_id.in_(source_ids)
                )
                .delete(synchronize_session=False)
            )
            if rows_to_store:
                db_manager.add_all(rows_to_store)

            current_dirty_ids = {
                int(adventurer_id)
                for adventurer_id in (state.dirty_adventurer_ids_json or [])
                if int(adventurer_id) > 0
            }
            if int(state.dirty_revision or 0) == spec['dirty_revision']:
                current_dirty_ids -= source_ids
            else:
                state.scan_total_adventurers = max(
                    int(state.scan_total_adventurers or 0),
                    int(state.scan_processed_adventurers or 0)
                    + len(source_ids)
                    + len(current_dirty_ids),
                )
            state.dirty_adventurer_ids_json = sorted(current_dirty_ids)
            if spec['batch_kind'] == 'audit_sources':
                state.scan_cursor_adventurer_id = max(source_ids)
            state.scan_processed_adventurers = (
                int(state.scan_processed_adventurers or 0) + len(source_ids)
            )
            state.scan_claim_token = None
            state.scan_claimed_at = None
            state.scan_last_error = None

            has_more_sources = bool(
                S1BugReclaimService._next_scan_source_adventurer_ids(
                    after_audit_id=spec['base_audit_log_id'],
                    through_audit_id=spec['target_audit_log_id'],
                    after_adventurer_id=int(state.scan_cursor_adventurer_id or 0),
                    limit=1,
                )
            )
            complete = not has_more_sources and not current_dirty_ids
            if complete:
                state.scan_status = 'ready'
                state.last_audit_log_id = spec['target_audit_log_id']
                final_progress = max(
                    int(state.scan_total_adventurers or 0),
                    int(state.scan_processed_adventurers or 0),
                )
                state.scan_total_adventurers = final_progress
                state.scan_processed_adventurers = final_progress

            new_audit_rows = sum(
                1 for audit, _appeal in history_rows
                if int(audit.id) > spec['base_audit_log_id']
            )
            return S1BugReclaimService._scan_progress_meta(
                state,
                complete=complete,
                processed_this_batch=len(source_ids),
                audit_rows=len(history_rows),
                new_audit_rows=new_audit_rows,
                refreshed_candidates=len(rows_to_store),
                dirty_adventurers=(
                    len(source_ids) if spec['batch_kind'] == 'dirty' else 0
                ),
                skipped_by_flow=flow_skipped,
                cluster_meta=cluster_meta,
            )

    @staticmethod
    def _refresh_incremental_candidates() -> dict:
        """每次请求推进一个按审计行预算约束的领取批次；完成后再发布候选池。"""
        from extensions import db

        for _attempt in range(3):
            spec, ready_meta = S1BugReclaimService._claim_scan_batch()
            if ready_meta is not None:
                return ready_meta
            if spec is None:
                continue

            try:
                history_rows, excluded = S1BugReclaimService._read_scan_batch(spec)
                raw_candidates, cluster_meta = S1BugReclaimService._build_cluster_candidates(
                    excluded, history_rows,
                )
                flow_rows = history_rows
                if raw_candidates:
                    flow_rows = S1BugReclaimService._read_scan_flow_rows(
                        spec,
                        history_rows,
                        raw_candidates,
                    )
                eligible, flow_skipped = S1BugReclaimService._apply_post_appeal_flow_filter(
                    raw_candidates, flow_rows,
                )

                # 过滤器会发起只读查询并自动开启事务；计算结束后必须先释放连接。
                db.session.rollback()
                source_ids = set(spec['source_adventurer_ids'])
                pending_rows = []
                for candidate in eligible:
                    source_adventurer_id = (
                        S1BugReclaimService._candidate_source_adventurer_id(candidate)
                    )
                    if source_adventurer_id not in source_ids:
                        raise RuntimeError(
                            '异常回收候选源冒险者越过已领取批次，已拒绝写回'
                        )
                    pending_rows.append(AdventurerS1BugReclaimCandidate(
                        issue_key=str(candidate['issue_key']),
                        adventurer_id=int(candidate['adventurer_id']),
                        source_adventurer_id=source_adventurer_id,
                        audit_log_id=(
                            int(candidate['audit_log_id'])
                            if candidate.get('audit_log_id') else None
                        ),
                        illegal_quantity=int(candidate.get('illegal_quantity') or 0),
                        detection_mode=_DETECTION_MODE,
                        payload_json=S1BugReclaimService._candidate_to_json(candidate),
                    ))

                persisted = S1BugReclaimService._persist_scan_batch(
                    spec,
                    pending_rows,
                    history_rows=history_rows,
                    cluster_meta=cluster_meta,
                    flow_skipped=flow_skipped,
                )
                if persisted is not None:
                    return persisted
            except Exception as exc:
                db.session.rollback()
                S1BugReclaimService._release_failed_scan_claim(
                    spec['claim_token'], exc,
                )
                raise

        raise RuntimeError('异常回收扫描状态持续变化，请稍后重试')

    @staticmethod
    def _load_pending_candidates(limit: int) -> tuple[list[dict], int]:
        query = db_manager.query(AdventurerS1BugReclaimCandidate).filter(
            AdventurerS1BugReclaimCandidate.detection_mode == _DETECTION_MODE,
        )
        total = query.count()
        rows = (
            query.order_by(
                AdventurerS1BugReclaimCandidate.illegal_quantity.desc(),
                AdventurerS1BugReclaimCandidate.issue_key.asc(),
            )
            .limit(limit)
            .all()
        )
        return [S1BugReclaimService._candidate_from_json(row.payload_json) for row in rows], total

    @staticmethod
    def preview(limit: int = 50, admin_user_id: int | None = None) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        try:
            scan_meta = S1BugReclaimService._refresh_incremental_candidates()
        except Exception as exc:
            return None, f'异常回收增量扫描失败：{exc}'
        if scan_meta.get('complete') is False:
            summary = S1BugReclaimService._summary([])
            summary.update({
                'total_candidates': 0,
                'raw_cluster_count': 0,
                'shown_count': 0,
                'limit': limit,
                'excluded_count': 0,
                'adjusted_count': 0,
                'scan': scan_meta,
                'detection_mode': _DETECTION_MODE,
            })
            return {'items': [], 'summary': summary}, None
        candidates, total_candidates = S1BugReclaimService._load_pending_candidates(limit)

        adv_ids = {int(c['adventurer_id']) for c in candidates}
        user_ids = {int(c['user_id']) for c in candidates if c.get('user_id')}
        adjustments = S1BugReclaimService._issue_adjustments()
        adv_map = S1BugReclaimService._adventurer_map(adv_ids)
        user_map = S1BugReclaimService._user_map(user_ids)
        items = [
            S1BugReclaimService._enrich_preview_item(c, adjustments, adv_map, user_map)
            for c in candidates
        ]
        summary = S1BugReclaimService._summary(items)
        summary['total_candidates'] = total_candidates
        summary['raw_cluster_count'] = total_candidates
        summary['shown_count'] = len(items)
        summary['limit'] = limit
        summary['excluded_count'] = len(S1BugReclaimService._executed_issue_keys())
        summary['adjusted_count'] = sum(1 for i in items if i.get('quantity_adjusted'))
        summary['text_correction_count'] = sum(
            1 for i in items if i.get('reclaim_mode') == 'text_correction'
        )
        summary['scan'] = scan_meta
        summary['detection_mode'] = _DETECTION_MODE
        result = {'items': items, 'summary': summary}
        if admin_user_id is not None:
            result['snapshot_token'] = S1AdminScanSnapshotService.store(
                'bug_reclaims', admin_user_id, result,
            )
        return result, None

    @staticmethod
    def _summary(items: list[dict]) -> dict:
        return {
            'count': len(items),
            'illegal_quantity': sum(int(i.get('illegal_quantity') or 0) for i in items),
            'reclaim_item_quantity': sum(int(i.get('reclaim_item_quantity') or 0) for i in items),
            'gold_to_deduct': round(sum(float(i.get('gold_to_deduct') or 0) for i in items), 2),
            'buyer_refund_gold': round(sum(float(i.get('buyer_refund_gold') or 0) for i in items), 2),
            'seller_clawback_gold': round(sum(float(i.get('seller_clawback_gold') or 0) for i in items), 2),
            'market_undo_count': sum(1 for i in items if i.get('reclaim_mode') == 'market_purchase_undo'),
            'text_correction_count': sum(1 for i in items if i.get('reclaim_mode') == 'text_correction'),
        }

    @staticmethod
    def execute(
        admin_user_id: int,
        limit: int = 50,
        note: str = '',
        preview_token: str = '',
    ) -> tuple:
        preview = None
        if preview_token:
            preview = S1AdminScanSnapshotService.load('bug_reclaims', admin_user_id, preview_token)
            if preview is None:
                return None, '预览快照已过期，请重新预览后再执行'
            preview_mode = (preview.get('summary') or {}).get('detection_mode')
            if preview_mode != _DETECTION_MODE:
                return None, '回收判定规则已更新，请重新预览后再执行'
        if preview is None:
            preview, err = S1BugReclaimService.preview(limit=limit)
            if err:
                return None, err
        scan_meta = (preview.get('summary') or {}).get('scan') or {}
        if scan_meta.get('complete') is False:
            return None, '异常回收预览仍在扫描，请等待预览完成后再执行'
        items = preview.get('items') or []
        if preview_token and items:
            adjustments = S1BugReclaimService._issue_adjustments()
            adv_ids = {int(item['adventurer_id']) for item in items}
            user_ids = {int(item['user_id']) for item in items if item.get('user_id')}
            adv_map = S1BugReclaimService._adventurer_map(adv_ids)
            user_map = S1BugReclaimService._user_map(user_ids)
            items = [
                S1BugReclaimService._enrich_preview_item(item, adjustments, adv_map, user_map)
                for item in items
            ]
        now = _portable_utc_now()
        batch = AdventurerS1BugReclaimBatch(
            admin_user_id=admin_user_id,
            status='executed',
            criteria_json={'limit': limit, 'mode': _DETECTION_MODE},
            summary_json=preview.get('summary') or {},
            note=(note or '').strip()[:200] or None,
            executed_at=now,
        )
        results = []
        try:
            with db_manager.transaction():
                S1BugReclaimService._lock_incremental_scan_transaction()
                current_scan_state = (
                    db_manager.query(AdventurerS1BugReclaimScanState)
                    .filter_by(id=1)
                    .with_for_update()
                    .first()
                )
                expected_audit_id = int(scan_meta.get('cursor_after') or 0)
                if (
                    current_scan_state is None
                    or current_scan_state.detection_mode != _DETECTION_MODE
                    or current_scan_state.scan_status != 'ready'
                    or bool(current_scan_state.dirty_adventurer_ids_json or [])
                    or int(current_scan_state.last_audit_log_id or 0) != expected_audit_id
                    or S1BugReclaimService._max_approved_audit_id()
                    != expected_audit_id
                ):
                    return None, '扫描水位已变化，请重新预览后再执行'
                db_manager.add(batch)
                issue_keys = [str(item.get('issue_key') or '') for item in items if item.get('issue_key')]
                existing_issue_keys = set()
                if issue_keys:
                    existing_issue_keys = {
                        row[0] for row in db_manager.query(
                            AdventurerS1BugReclaimItem.issue_key,
                        ).filter(
                            AdventurerS1BugReclaimItem.issue_key.in_(issue_keys),
                            AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
                        ).all()
                    }
                for item in items:
                    result = S1BugReclaimService._execute_one(
                        item, batch, admin_user_id, now, existing_issue_keys,
                    )
                    results.append(result)
                processed_keys = [
                    str(result.get('issue_key')) for result in results
                    if result.get('status') == 'executed' and result.get('issue_key')
                ]
                if processed_keys:
                    (
                        db_manager.query(AdventurerS1BugReclaimCandidate)
                        .filter(AdventurerS1BugReclaimCandidate.issue_key.in_(processed_keys))
                        .delete(synchronize_session=False)
                    )
        except Exception as exc:
            return None, f'回收失败：{exc}'
        S1AdminScanSnapshotService.delete('bug_reclaims', admin_user_id, preview_token)
        return {'batch': batch.to_dict(), 'items': results, 'summary': S1BugReclaimService._summary(results)}, None

    @staticmethod
    def dismiss(admin_user_id: int, issue_key: str, item: dict | None = None, note: str = '') -> tuple:
        """管理员标记预览项为误报，持久化后不再出现在预览/执行列表。"""
        key = str(issue_key or '').strip()
        if not key:
            return None, 'issue_key 不能为空'

        existing = (
            db_manager.query(AdventurerS1BugReclaimItem.id, AdventurerS1BugReclaimItem.status)
            .filter(
                AdventurerS1BugReclaimItem.issue_key == key,
                AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
            )
            .first()
        )
        if existing:
            return None, '该条目已回收或已标记误报'

        payload = item if isinstance(item, dict) else {}
        now = _portable_utc_now()
        batch = AdventurerS1BugReclaimBatch(
            admin_user_id=admin_user_id,
            status='dismissed',
            criteria_json={'issue_key': key, 'mode': 'dismiss_false_positive'},
            summary_json={
                'action': 'dismiss_false_positive',
                'issue_key': key,
                'item_name': payload.get('item_name'),
                'illegal_quantity': int(payload.get('illegal_quantity') or 0),
            },
            note=(note or '管理员标记误报').strip()[:200],
            executed_at=now,
        )
        row = AdventurerS1BugReclaimItem(
            batch=batch,
            status='dismissed',
            issue_key=key,
            issue_type=str(payload.get('issue_type') or 'quantity_cluster'),
            appeal_id=payload.get('appeal_id'),
            audit_log_id=payload.get('audit_log_id'),
            adventurer_id=int(payload.get('adventurer_id') or 0),
            user_id=payload.get('user_id'),
            item_name=str(payload.get('item_name') or key)[:100],
            item_type=str(payload.get('item_type') or '')[:50],
            item_rarity=str(payload.get('item_rarity') or 'common')[:20],
            illegal_quantity=int(payload.get('illegal_quantity') or 0),
            reclaimed_item_quantity=0,
            gold_deducted=0.0,
            before_json=payload.get('before') or {},
            after_json=payload.get('after') or {},
            route_json=payload.get('route') or [],
            result_json={'message': '管理员标记误报', 'note': (note or '').strip()[:200]},
            executed_by=admin_user_id,
            executed_at=now,
        )
        if row.adventurer_id <= 0:
            return None, '缺少有效的冒险者信息，请刷新预览后重试'

        try:
            with db_manager.transaction():
                S1BugReclaimService._lock_incremental_scan_transaction()
                existing = (
                    db_manager.query(AdventurerS1BugReclaimItem.id)
                    .filter(
                        AdventurerS1BugReclaimItem.issue_key == key,
                        AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
                    )
                    .first()
                )
                if existing:
                    return None, '该条目已回收或已标记误报'
                db_manager.add(batch)
                db_manager.add(row)
                (
                    db_manager.query(AdventurerS1BugReclaimCandidate)
                    .filter(AdventurerS1BugReclaimCandidate.issue_key == key)
                    .delete(synchronize_session=False)
                )
        except Exception as exc:
            return None, f'标记误报失败：{exc}'

        return {
            'batch': batch.to_dict(),
            'item': row.to_dict(),
        }, None

    @staticmethod
    def adjust_quantity(
        admin_user_id: int,
        issue_key: str,
        illegal_quantity: int,
        item: dict | None = None,
        note: str = '',
    ) -> tuple:
        """管理员认定非法数量（可大于或小于系统累计值）；持久化后预览/执行按认定值扣物扣金。"""
        key = str(issue_key or '').strip()
        if not key:
            return None, 'issue_key 不能为空'

        try:
            qty = int(illegal_quantity)
        except (TypeError, ValueError):
            return None, '非法数量必须是整数'
        if qty < 0:
            return None, '非法数量不能为负'

        blocked = (
            db_manager.query(AdventurerS1BugReclaimItem.id)
            .filter(
                AdventurerS1BugReclaimItem.issue_key == key,
                AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
            )
            .first()
        )
        if blocked:
            return None, '该条目已回收或已标记误报'

        payload = item if isinstance(item, dict) else {}
        if payload.get('quantity_adjusted'):
            computed = int(payload.get('computed_illegal_quantity') or 0)
        else:
            computed = int(payload.get('illegal_quantity') or 0)
        if computed <= 0:
            return None, '缺少有效的系统计算非法量，请刷新预览后重试'

        existing_adj = (
            db_manager.query(AdventurerS1BugReclaimItem)
            .filter(
                AdventurerS1BugReclaimItem.issue_key == key,
                AdventurerS1BugReclaimItem.status == 'adjusted',
            )
            .first()
        )

        if qty == computed:
            if existing_adj:
                try:
                    with db_manager.transaction():
                        ok, del_err = db_manager.delete_instance(existing_adj, flush=False)
                        if not ok:
                            raise RuntimeError(del_err or '删除旧认定失败')
                except Exception as exc:
                    return None, f'恢复系统计算值失败：{exc}'
            preview_item = S1BugReclaimService._preview_item_from_payload(payload, computed, computed)
            return {
                'reverted': True,
                'item': preview_item,
            }, None

        now = _portable_utc_now()
        note_text = (note or '').strip()[:200]
        result_meta = {
            'action': 'adjust_illegal_quantity',
            'computed_illegal_quantity': computed,
            'note': note_text,
        }
        adv_id = int(payload.get('adventurer_id') or 0)
        if adv_id <= 0:
            return None, '缺少有效的冒险者信息，请刷新预览后重试'

        try:
            with db_manager.transaction():
                if existing_adj:
                    existing_adj.illegal_quantity = qty
                    existing_adj.before_json = payload
                    existing_adj.result_json = result_meta
                    existing_adj.executed_by = admin_user_id
                    existing_adj.executed_at = now
                    row = existing_adj
                else:
                    batch = AdventurerS1BugReclaimBatch(
                        admin_user_id=admin_user_id,
                        status='adjusted',
                        criteria_json={'issue_key': key, 'mode': 'adjust_illegal_quantity'},
                        summary_json={
                            'action': 'adjust_illegal_quantity',
                            'issue_key': key,
                            'item_name': payload.get('item_name'),
                            'computed_illegal_quantity': computed,
                            'illegal_quantity': qty,
                        },
                        note=note_text or (
                            f'认定非法 +{qty}（系统 +{computed}）'
                            if qty < computed
                            else f'认定非法 +{qty}（系统 +{computed}，上调）'
                        ),
                        executed_at=now,
                    )
                    row = AdventurerS1BugReclaimItem(
                        batch=batch,
                        status='adjusted',
                        issue_key=key,
                        issue_type=str(payload.get('issue_type') or 'quantity_cluster'),
                        appeal_id=payload.get('appeal_id'),
                        audit_log_id=payload.get('audit_log_id'),
                        adventurer_id=adv_id,
                        user_id=payload.get('user_id'),
                        item_name=str(payload.get('item_name') or key)[:100],
                        item_type=str(payload.get('item_type') or '')[:50],
                        item_rarity=str(payload.get('item_rarity') or 'common')[:20],
                        illegal_quantity=qty,
                        reclaimed_item_quantity=0,
                        gold_deducted=0.0,
                        before_json=payload,
                        after_json={},
                        route_json=[],
                        result_json=result_meta,
                        executed_by=admin_user_id,
                        executed_at=now,
                    )
                    db_manager.add(batch)
                    db_manager.add(row)
        except Exception as exc:
            return None, f'认定非法数量失败：{exc}'

        preview_item = S1BugReclaimService._preview_item_from_payload(payload, computed, qty, note_text)
        return {
            'reverted': False,
            'batch': row.batch.to_dict() if row.batch else None,
            'item': preview_item,
        }, None

    @staticmethod
    def _get_existing_adjustment(issue_key: str):
        return (
            db_manager.query(AdventurerS1BugReclaimItem)
            .filter(
                AdventurerS1BugReclaimItem.issue_key == issue_key,
                AdventurerS1BugReclaimItem.status == 'adjusted',
            )
            .first()
        )

    @staticmethod
    def _delete_existing_adjustment(issue_key: str) -> None:
        row = S1BugReclaimService._get_existing_adjustment(issue_key)
        if not row:
            return
        ok, del_err = db_manager.delete_instance(row, flush=False)
        if not ok:
            raise RuntimeError(del_err or '删除旧认定失败')

    @staticmethod
    def adjust_text(
        admin_user_id: int,
        issue_key: str,
        corrected_name: str,
        corrected_desc: str | None = None,
        corrected_quantity: int | None = None,
        item: dict | None = None,
        note: str = '',
        revert: bool = False,
    ) -> tuple:
        """管理员认定文字修正：执行时改目标堆 name/desc（可选 quantity），不扣物扣金。"""
        key = str(issue_key or '').strip()
        if not key:
            return None, 'issue_key 不能为空'

        blocked = (
            db_manager.query(AdventurerS1BugReclaimItem.id)
            .filter(
                AdventurerS1BugReclaimItem.issue_key == key,
                AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
            )
            .first()
        )
        if blocked:
            return None, '该条目已回收或已标记误报'

        payload = item if isinstance(item, dict) else {}
        adv_id = int(payload.get('adventurer_id') or 0)
        if adv_id <= 0:
            return None, '缺少有效的冒险者信息，请刷新预览后重试'

        if revert:
            try:
                with db_manager.transaction():
                    S1BugReclaimService._delete_existing_adjustment(key)
            except Exception as exc:
                return None, f'取消文字修正失败：{exc}'
            base_payload = {
                k: v for k, v in payload.items()
                if k not in (
                    'reclaim_mode', 'text_correction', 'route', 'reclaim_item_quantity',
                    'gold_to_deduct', 'buyer_refund_gold', 'seller_clawback_gold',
                    'quantity_adjusted', 'adjustment_note',
                )
            }
            computed = int(
                base_payload.get('computed_illegal_quantity')
                or base_payload.get('illegal_quantity') or 0
            )
            effective = int(base_payload.get('illegal_quantity') or computed)
            preview_item = S1BugReclaimService._preview_item_from_payload(
                base_payload, computed, effective,
            )
            return {'reverted': True, 'item': preview_item}, None

        name = str(corrected_name or '').strip()
        if not name:
            return None, '修正名称不能为空'
        if len(name) > 100:
            return None, '名称过长'

        desc = None if corrected_desc is None else str(corrected_desc).strip()
        qty = None
        if corrected_quantity is not None and str(corrected_quantity).strip() != '':
            try:
                qty = int(corrected_quantity)
            except (TypeError, ValueError):
                return None, '修正数量必须是整数'
            if qty < 1:
                return None, '修正数量至少为 1'

        now = _portable_utc_now()
        note_text = (note or '').strip()[:200]
        result_meta = {
            'action': 'adjust_item_text',
            'corrected_name': name,
            'corrected_desc': desc,
            'corrected_quantity': qty,
            'note': note_text,
        }
        existing_adj = S1BugReclaimService._get_existing_adjustment(key)

        try:
            with db_manager.transaction():
                if existing_adj:
                    existing_adj.illegal_quantity = 0
                    existing_adj.before_json = payload
                    existing_adj.result_json = result_meta
                    existing_adj.executed_by = admin_user_id
                    existing_adj.executed_at = now
                    row = existing_adj
                else:
                    batch = AdventurerS1BugReclaimBatch(
                        admin_user_id=admin_user_id,
                        status='adjusted',
                        criteria_json={'issue_key': key, 'mode': 'adjust_item_text'},
                        summary_json={
                            'action': 'adjust_item_text',
                            'issue_key': key,
                            'item_name': payload.get('item_name'),
                            'corrected_name': name,
                        },
                        note=note_text or f'文字修正→{name}',
                        executed_at=now,
                    )
                    row = AdventurerS1BugReclaimItem(
                        batch=batch,
                        status='adjusted',
                        issue_key=key,
                        issue_type=str(payload.get('issue_type') or 'quantity_cluster'),
                        appeal_id=payload.get('appeal_id'),
                        audit_log_id=payload.get('audit_log_id'),
                        adventurer_id=adv_id,
                        user_id=payload.get('user_id'),
                        item_name=str(payload.get('item_name') or key)[:100],
                        item_type=str(payload.get('item_type') or '')[:50],
                        item_rarity=str(payload.get('item_rarity') or 'common')[:20],
                        illegal_quantity=0,
                        reclaimed_item_quantity=0,
                        gold_deducted=0.0,
                        before_json=payload,
                        after_json={},
                        route_json=[],
                        result_json=result_meta,
                        executed_by=admin_user_id,
                        executed_at=now,
                    )
                    db_manager.add(batch)
                    db_manager.add(row)
        except Exception as exc:
            return None, f'保存文字修正失败：{exc}'

        adj = {
            'mode': 'text_correction',
            'corrected_name': name,
            'corrected_desc': desc,
            'corrected_quantity': qty,
            'note': note_text,
        }
        preview_item = {
            **payload,
            **S1BugReclaimService._plan_text_correction(payload, adj),
        }
        return {
            'reverted': False,
            'batch': row.batch.to_dict() if row.batch else None,
            'item': preview_item,
        }, None

    @staticmethod
    def _trace_since_from_payload(payload: dict):
        raw = payload.get('_since') or payload.get('trace_since')
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw
        from utils.time_utils import parse_iso_date
        return parse_iso_date(str(raw))

    @staticmethod
    def _preview_item_from_payload(
        payload: dict,
        computed: int,
        effective: int,
        adjustment_note: str = '',
    ) -> dict:
        """根据前端提交的预览项快照，重算扣物/扣金（认定数量变更后即时反馈）。"""
        trace_candidate = {**payload, 'illegal_quantity': effective}
        since = S1BugReclaimService._trace_since_from_payload(payload)
        if since is not None:
            trace_candidate['_since'] = since
        trace = S1BugReclaimService._trace_route(trace_candidate)
        out = {
            **payload,
            'computed_illegal_quantity': computed,
            'illegal_quantity': effective,
            'quantity_adjusted': effective != computed,
            **trace,
        }
        if effective != computed:
            out['adjustment_note'] = adjustment_note
        else:
            out.pop('quantity_adjusted', None)
            out.pop('adjustment_note', None)
        return out

    @staticmethod
    def _item_restore_snapshot(source: str, row, quantity: int) -> dict:
        """冻结本次实际扣除的物品实例属性，供单条回溯精确恢复。"""
        return {
            'source': source,
            'row_id': int(row.id),
            'name': str(row.name or ''),
            'type': str(row.type or ''),
            'rarity': str(row.rarity or 'common'),
            'quantity': int(quantity),
            'uses_left': row.uses_left,
            'custom_desc': row.custom_desc,
            'evolution_stage': int(getattr(row, 'evolution_stage', 0) or 0),
        }

    @staticmethod
    def _execute_one(
        item: dict,
        batch: AdventurerS1BugReclaimBatch,
        admin_user_id: int,
        now: datetime,
        existing_issue_keys: set[str] | None = None,
    ) -> dict:
        issue_key = item['issue_key']
        if existing_issue_keys is None:
            existing = db_manager.query(AdventurerS1BugReclaimItem.id).filter(
                AdventurerS1BugReclaimItem.issue_key == issue_key,
                AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')),
            ).first()
            already_processed = existing is not None
        else:
            already_processed = issue_key in existing_issue_keys
        if already_processed:
            return {**item, 'status': 'skipped', 'result': {'message': '已处理或已标记误报，跳过'}}

        reclaim_mode = str(item.get('reclaim_mode') or 'default')
        if reclaim_mode == 'market_purchase_undo':
            return S1BugReclaimService._execute_market_purchase_undo(
                item, batch, admin_user_id, now,
            )
        if reclaim_mode == 'text_correction':
            return S1BugReclaimService._execute_text_correction(
                item, batch, admin_user_id, now,
            )

        to_remove = int(item.get('reclaim_item_quantity') or 0)
        removed = 0
        consumed_items = []
        if to_remove > 0:
            for source, row in S1BugReclaimService._current_items(item, all_matching=True):
                if removed >= to_remove:
                    break
                amount = min(to_remove - removed, item_quantity_of(row))
                consumed_items.append(S1BugReclaimService._item_restore_snapshot(source, row, amount))
                _deleted, err = consume_item_stack(
                    row,
                    amount,
                    source_type='bug_reclaim',
                    description=S1BugReclaimService._item_reclaim_log_description(item, amount),
                    hidden_from_player=False,
                )
                if err:
                    raise RuntimeError(err)
                removed += amount

        gold = round(float(item.get('gold_to_deduct') or 0), 2)
        balance_after: Optional[float] = None
        if gold > 0:
            balance_after, err = S1CurrencyService.deduct(
                int(item['adventurer_id']),
                'gold',
                gold,
                source_type='bug_reclaim',
                related_id=batch.id,
                related_type='bug_reclaim_batch',
                description=S1BugReclaimService._gold_reclaim_log_description(item),
                auto_commit=False,
                allow_negative=True,
            )
            if err:
                raise RuntimeError(err)

        result_json = {
            'removed_item_quantity': removed,
            'gold_deducted': gold,
            'balance_after': balance_after,
            'revert_snapshot': {
                'version': 1,
                'mode': 'default',
                'consumed_items': consumed_items,
                'gold_deducted': gold,
            },
        }
        row = AdventurerS1BugReclaimItem(
            batch=batch,
            status='executed',
            issue_key=issue_key,
            issue_type=item['issue_type'],
            appeal_id=item.get('appeal_id'),
            audit_log_id=item.get('audit_log_id'),
            adventurer_id=int(item['adventurer_id']),
            user_id=item.get('user_id'),
            item_name=item['item_name'],
            item_type=item.get('item_type') or '',
            item_rarity=item.get('item_rarity') or 'common',
            illegal_quantity=int(item.get('illegal_quantity') or 0),
            reclaimed_item_quantity=removed,
            gold_deducted=gold,
            before_json=item.get('before') or {},
            after_json=item.get('after') or {},
            route_json=item.get('route') or [],
            result_json=result_json,
            executed_by=admin_user_id,
            executed_at=now,
        )
        db_manager.add(row)
        return {**item, 'status': 'executed', 'result': result_json}

    @staticmethod
    def _execute_market_purchase_undo(
        item: dict,
        batch: AdventurerS1BugReclaimBatch,
        admin_user_id: int,
        now: datetime,
    ) -> dict:
        prov = item.get('market_provenance') or {}
        removed = 0
        consumed_items = []
        for source, row in S1BugReclaimService._current_items(item):
            amount = item_quantity_of(row)
            if amount <= 0:
                continue
            consumed_items.append(S1BugReclaimService._item_restore_snapshot(source, row, amount))
            _, err = consume_item_stack(
                row,
                amount,
                source_type='bug_reclaim',
                description=(
                    f'管理员撤销市场购入「{item["item_name"]}」×{amount}'
                    f'（卖家 #{prov.get("seller_adventurer_id")}）'
                ),
                hidden_from_player=False,
            )
            if err:
                raise RuntimeError(err)
            removed += amount

        buyer_refund = round(float(item.get('buyer_refund_gold') or prov.get('buyer_paid') or 0), 2)
        seller_claw = round(float(item.get('seller_clawback_gold') or prov.get('seller_income') or 0), 2)
        buyer_balance_after: Optional[float] = None
        seller_balance_after: Optional[float] = None

        if buyer_refund > 0:
            buyer_balance_after, err = S1CurrencyService.add(
                int(item['adventurer_id']),
                'gold',
                buyer_refund,
                source_type='bug_reclaim',
                related_id=batch.id,
                related_type='bug_reclaim_batch',
                description=f'管理员撤销市场购入退款「{item["item_name"]}」',
                auto_commit=False,
            )
            if err:
                raise RuntimeError(err)

        if seller_claw > 0:
            seller_id = int(prov.get('seller_adventurer_id') or 0)
            if seller_id <= 0:
                raise RuntimeError('缺少卖家冒险者信息，无法扣回售出金币')
            seller_balance_after, err = S1CurrencyService.deduct(
                seller_id,
                'gold',
                seller_claw,
                source_type='bug_reclaim',
                related_id=batch.id,
                related_type='bug_reclaim_batch',
                description=f'管理员撤销市场售出扣回「{item["item_name"]}」',
                auto_commit=False,
                allow_negative=True,
            )
            if err:
                raise RuntimeError(err)

        result_json = {
            'reclaim_mode': 'market_purchase_undo',
            'removed_item_quantity': removed,
            'buyer_refund_gold': buyer_refund,
            'seller_clawback_gold': seller_claw,
            'buyer_balance_after': buyer_balance_after,
            'seller_balance_after': seller_balance_after,
            'market_provenance': prov,
            'revert_snapshot': {
                'version': 1,
                'mode': 'market_purchase_undo',
                'consumed_items': consumed_items,
                'buyer_refund_gold': buyer_refund,
                'seller_clawback_gold': seller_claw,
                'seller_adventurer_id': int(prov.get('seller_adventurer_id') or 0),
            },
        }
        row = AdventurerS1BugReclaimItem(
            batch=batch,
            status='executed',
            issue_key=item['issue_key'],
            issue_type=item['issue_type'],
            appeal_id=item.get('appeal_id'),
            audit_log_id=item.get('audit_log_id'),
            adventurer_id=int(item['adventurer_id']),
            user_id=item.get('user_id'),
            item_name=item['item_name'],
            item_type=item.get('item_type') or '',
            item_rarity=item.get('item_rarity') or 'common',
            illegal_quantity=int(item.get('illegal_quantity') or 0),
            reclaimed_item_quantity=removed,
            gold_deducted=0.0,
            before_json=item,
            after_json={'buyer_refund_gold': buyer_refund, 'seller_clawback_gold': seller_claw},
            route_json=item.get('route') or [],
            result_json=result_json,
            executed_by=admin_user_id,
            executed_at=now,
        )
        db_manager.add(row)
        return {**item, 'status': 'executed', 'result': result_json}

    @staticmethod
    def _execute_text_correction(
        item: dict,
        batch: AdventurerS1BugReclaimBatch,
        admin_user_id: int,
        now: datetime,
    ) -> dict:
        tc = item.get('text_correction') or {}
        name = str(tc.get('corrected_name') or '').strip()
        if not name:
            raise RuntimeError('缺少文字修正名称')
        desc = tc.get('corrected_desc')
        qty = tc.get('corrected_quantity')

        from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity

        before_state = None
        after_state = None
        updated = False
        for source, row in S1BugReclaimService._current_items(item):
            before_state = {
                'source': source,
                'id': int(row.id),
                'name': row.name,
                'custom_desc': row.custom_desc,
                'quantity': item_quantity_of(row),
            }
            row.name = name[:100]
            if desc is not None:
                row.custom_desc = str(desc).strip() or None
            if qty is not None:
                row.quantity = coerce_item_quantity(qty, 1)
            after_state = {
                'source': source,
                'id': int(row.id),
                'name': row.name,
                'custom_desc': row.custom_desc,
                'quantity': item_quantity_of(row),
            }
            updated = True
            break

        if not updated:
            raise RuntimeError('未找到目标物品堆，无法执行文字修正')

        result_json = {
            'reclaim_mode': 'text_correction',
            'text_correction': tc,
            'before': before_state,
            'after': after_state,
        }
        row = AdventurerS1BugReclaimItem(
            batch=batch,
            status='executed',
            issue_key=item['issue_key'],
            issue_type=item['issue_type'],
            appeal_id=item.get('appeal_id'),
            audit_log_id=item.get('audit_log_id'),
            adventurer_id=int(item['adventurer_id']),
            user_id=item.get('user_id'),
            item_name=name[:100],
            item_type=item.get('item_type') or '',
            item_rarity=item.get('item_rarity') or 'common',
            illegal_quantity=int(item.get('illegal_quantity') or 0),
            reclaimed_item_quantity=0,
            gold_deducted=0.0,
            before_json=before_state,
            after_json=after_state,
            route_json=item.get('route') or [],
            result_json=result_json,
            executed_by=admin_user_id,
            executed_at=now,
        )
        db_manager.add(row)
        return {**item, 'status': 'executed', 'result': result_json}

    @staticmethod
    def _bug_reclaim_status_map() -> dict[str, dict]:
        """issue_key → 最新回收台账状态（executed 优先于 dismissed/adjusted）。"""
        priority = {'executed': 3, 'dismissed': 2, 'adjusted': 1}
        rows = (
            db_manager.query(AdventurerS1BugReclaimItem)
            .filter(AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed', 'adjusted')))
            .all()
        )
        out: dict[str, dict] = {}
        for row in rows:
            key = str(row.issue_key or '')
            if not key:
                continue
            meta = row.result_json or {}
            action = str(meta.get('action') or '')
            entry = {
                'status': row.status,
                'action': action,
                'illegal_quantity': int(row.illegal_quantity or 0),
                'executed_at': to_utc_iso(row.executed_at) if row.executed_at else None,
                'reverted_at': to_utc_iso(row.reverted_at) if row.reverted_at else None,
            }
            prev = out.get(key)
            if not prev or priority.get(row.status, 0) > priority.get(prev.get('status'), 0):
                out[key] = entry
        return out

    @staticmethod
    def _resolve_reclaim_status_label(
        issue_key: str | None,
        reclaim_map: dict[str, dict],
        eligible_keys: set[str],
        illegal_keys: set[str],
    ) -> tuple[str, str]:
        if issue_key and issue_key in reclaim_map:
            st = reclaim_map[issue_key]['status']
            action = reclaim_map[issue_key].get('action') or ''
            if st == 'executed':
                if reclaim_map[issue_key].get('reverted_at'):
                    return 'reverted', '已回溯回收'
                if action == 'adjust_item_text':
                    return 'executed_text', '已执行文字修正'
                return 'executed', '已执行回收'
            if st == 'dismissed':
                return 'dismissed', '已标记误报'
            if st == 'adjusted':
                if action == 'adjust_item_text':
                    return 'adjusted_text', '已保存文字修正认定'
                return 'adjusted_qty', '已保存数量认定'
        if issue_key and issue_key in eligible_keys:
            return 'preview_eligible', '待回收（预览命中）'
        if issue_key and issue_key in illegal_keys:
            return 'detected_filtered', '已检出但未进预览'
        return 'none', '未进入异常回收'

    @staticmethod
    def _catalog_event_summary(
        op_type: str,
        before: dict,
        after: dict,
        *,
        change_kind: str,
    ) -> str:
        if op_type == 'create_item':
            qty = S1BugReclaimService._qty(after.get('quantity')) or 1
            dest = after.get('destination') or after.get('requested_destination') or 'warehouse'
            return f'新增 ×{qty} → {dest}'
        if change_kind == 'qty_increase':
            return f'数量 {S1BugReclaimService._qty(before.get("quantity"))} → {S1BugReclaimService._qty(after.get("quantity"))}（+{S1BugReclaimService._qty(after.get("quantity")) - S1BugReclaimService._qty(before.get("quantity"))}）'
        if change_kind == 'qty_decrease':
            return f'数量 {S1BugReclaimService._qty(before.get("quantity"))} → {S1BugReclaimService._qty(after.get("quantity"))}（{S1BugReclaimService._qty(after.get("quantity")) - S1BugReclaimService._qty(before.get("quantity"))}）'
        nb, na = before.get('name'), after.get('name')
        if nb is not None and na is not None and str(nb) != str(na):
            return f'改名 {nb} → {na}'
        return '字段更新'

    @staticmethod
    def _catalog_fuzzy_group_key(adventurer_id: int, item_name: str, item_type: str, item_rarity: str) -> tuple:
        return (
            int(adventurer_id),
            S1BugReclaimService._catalog_fuzzy_name(item_name),
            str(item_type or ''),
            str(item_rarity or 'common'),
        )

    @staticmethod
    def _filter_catalog_items_by_fuzzy_appeals(items: list[dict]) -> tuple[list[dict], dict]:
        """同名模糊合并后仅 1 次申诉的条目不展示（如仅申诉过「三枚abc」一次）。"""
        from collections import defaultdict

        appeal_ids_by_group: dict[tuple, set[int]] = defaultdict(set)
        names_by_group: dict[tuple, set[str]] = defaultdict(set)
        for item in items:
            gkey = S1BugReclaimService._catalog_fuzzy_group_key(
                item.get('adventurer_id') or 0,
                item.get('item_name') or '',
                item.get('item_type') or '',
                item.get('item_rarity') or 'common',
            )
            appeal_ids_by_group[gkey].update(item.get('appeal_ids') or [])
            name = str(item.get('item_name') or '').strip()
            if name:
                names_by_group[gkey].add(name)

        kept: list[dict] = []
        skipped = 0
        for item in items:
            gkey = S1BugReclaimService._catalog_fuzzy_group_key(
                item.get('adventurer_id') or 0,
                item.get('item_name') or '',
                item.get('item_type') or '',
                item.get('item_rarity') or 'common',
            )
            if len(appeal_ids_by_group[gkey]) <= 1:
                skipped += 1
                continue
            variants = sorted(names_by_group.get(gkey) or [])
            if len(variants) > 1:
                item['name_variants'] = variants
            item['fuzzy_name'] = gkey[1]
            kept.append(item)
        return kept, {'skipped_single_appeal': skipped, 'before_filter': len(items), 'after_filter': len(kept)}

    @staticmethod
    def _catalog_track_key_for_create(adventurer_id: int, after: dict) -> str:
        name = S1BugReclaimService._text(after.get('name'))
        norm = S1BugReclaimService._normalize_name(name)
        dest = str(after.get('destination') or after.get('requested_destination') or 'warehouse')
        item_type = str(after.get('type') or '')
        rarity = str(after.get('rarity') or 'common')
        return f'name:{adventurer_id}:{dest}:{norm}:{item_type}:{rarity}'

    @staticmethod
    def _catalog_track_key_for_slot(adventurer_id: int, target_type: str, target_id: int) -> str:
        return f'slot:{adventurer_id}:{target_type}:{int(target_id)}'

    @staticmethod
    def _catalog_issue_key(adventurer_id: int, target_type: str | None, target_id: int | None, after: dict) -> str | None:
        if target_type in ('warehouse', 'equipment') and target_id is not None:
            inner = S1BugReclaimService._cluster_key_target(adventurer_id, target_type, int(target_id))
            return f'cluster:{inner}'
        name = S1BugReclaimService._text(after.get('name'))
        if not name:
            return None
        inner = S1BugReclaimService._cluster_key_name(
            adventurer_id,
            S1BugReclaimService._normalize_name(name),
            str(after.get('type') or ''),
            str(after.get('rarity') or 'common'),
        )
        return f'cluster:{inner}'

    @staticmethod
    def item_change_catalog(limit: int = 500, sort: str = 'name') -> tuple:
        """全量申诉审计：新增物品 + 改 quantity，供管理员总览来源/申诉/回收状态。"""
        limit = max(1, min(int(limit or 500), 2000))
        rows = S1BugReclaimService._fetch_approved_audit_rows(include_catalog_text=True)
        reclaim_map = S1BugReclaimService._bug_reclaim_status_map()
        excluded = S1BugReclaimService._executed_issue_keys()
        raw_clusters, scan_meta = S1BugReclaimService._build_cluster_candidates(excluded, rows)
        eligible, flow_skipped = S1BugReclaimService._apply_post_appeal_flow_filter(raw_clusters, rows)
        eligible_keys = {str(c['issue_key']) for c in eligible}
        illegal_keys = {
            str(c['issue_key']) for c in raw_clusters if int(c.get('illegal_quantity') or 0) > 0
        }

        adv_ids: set[int] = set()
        user_ids: set[int] = set()
        tracks: dict[str, dict] = {}

        for audit, appeal in rows:
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            adventurer_id = int(audit.adventurer_id)
            adv_ids.add(adventurer_id)
            if appeal.user_id:
                user_ids.add(int(appeal.user_id))

            event_base = {
                'audit_log_id': int(audit.id),
                'appeal_id': int(appeal.id),
                'applied_at': to_utc_iso(appeal.applied_at) if appeal.applied_at else None,
                'audit_at': to_utc_iso(audit.created_at) if audit.created_at else None,
                'player_note': S1BugReclaimService._truncate_text(appeal.player_note, 300),
                'ai_summary': S1BugReclaimService._truncate_text(appeal.ai_summary, 300),
                'audit_note': S1BugReclaimService._truncate_text(audit.note, 200),
            }

            if audit.op_type == 'create_item':
                item_name = S1BugReclaimService._text(after.get('name'))
                if not item_name:
                    continue
                track_key = S1BugReclaimService._catalog_track_key_for_create(adventurer_id, after)
                track = tracks.setdefault(track_key, {
                    'track_key': track_key,
                    'track_kind': 'appeal_create',
                    'adventurer_id': adventurer_id,
                    'user_id': int(appeal.user_id) if appeal.user_id else None,
                    'item_name': item_name,
                    'item_type': str(after.get('type') or ''),
                    'item_rarity': str(after.get('rarity') or 'common'),
                    'target_type': str(after.get('destination') or after.get('requested_destination') or 'warehouse'),
                    'target_id': None,
                    'change_kinds': set(),
                    'events': [],
                    'appeal_ids': set(),
                    'create_count': 0,
                    'qty_increase_steps': 0,
                    'qty_decrease_steps': 0,
                })
                track['change_kinds'].add('create_item')
                track['create_count'] = int(track.get('create_count') or 0) + 1
                track['appeal_ids'].add(int(appeal.id))
                track['events'].append({
                    **event_base,
                    'op_type': 'create_item',
                    'change_kind': 'create_item',
                    'summary': S1BugReclaimService._catalog_event_summary(
                        'create_item', {}, after, change_kind='create_item',
                    ),
                    'qty_after': S1BugReclaimService._qty(after.get('quantity')),
                    'name_after': item_name,
                })
                continue

            if audit.op_type != 'update' or audit.target_type not in ('warehouse', 'equipment'):
                continue
            if 'quantity' not in after:
                continue
            old_qty = S1BugReclaimService._qty(before.get('quantity'))
            new_qty = S1BugReclaimService._qty(after.get('quantity'))
            if new_qty == old_qty:
                continue

            target_id = audit.target_id or before.get('id') or after.get('id')
            if target_id is None:
                continue
            target_type = str(audit.target_type)
            track_key = S1BugReclaimService._catalog_track_key_for_slot(
                adventurer_id, target_type, int(target_id),
            )
            item_name = S1BugReclaimService._text(after.get('name') or before.get('name'))
            change_kind = 'qty_increase' if new_qty > old_qty else 'qty_decrease'
            track = tracks.setdefault(track_key, {
                'track_key': track_key,
                'track_kind': 'slot_qty',
                'adventurer_id': adventurer_id,
                'user_id': int(appeal.user_id) if appeal.user_id else None,
                'item_name': item_name,
                'item_type': str(after.get('type') or before.get('type') or ''),
                'item_rarity': str(after.get('rarity') or before.get('rarity') or 'common'),
                'target_type': target_type,
                'target_id': int(target_id),
                'change_kinds': set(),
                'events': [],
                'appeal_ids': set(),
                'create_count': 0,
                'qty_increase_steps': 0,
                'qty_decrease_steps': 0,
            })
            track['change_kinds'].add(change_kind)
            if change_kind == 'qty_increase':
                track['qty_increase_steps'] = int(track.get('qty_increase_steps') or 0) + 1
            else:
                track['qty_decrease_steps'] = int(track.get('qty_decrease_steps') or 0) + 1
            track['appeal_ids'].add(int(appeal.id))
            if item_name:
                track['item_name'] = item_name
            track['events'].append({
                **event_base,
                'op_type': 'update',
                'change_kind': change_kind,
                'summary': S1BugReclaimService._catalog_event_summary(
                    'update', before, after, change_kind=change_kind,
                ),
                'qty_before': old_qty,
                'qty_after': new_qty,
                'name_before': before.get('name'),
                'name_after': after.get('name'),
            })

        adv_map = S1BugReclaimService._adventurer_map(adv_ids)
        user_map = S1BugReclaimService._user_map(user_ids)
        items: list[dict] = []

        for track in tracks.values():
            track['change_kinds'] = sorted(track['change_kinds'])
            track['appeal_ids'] = sorted(track['appeal_ids'])
            track['appeal_count'] = len(track['appeal_ids'])
            track['event_count'] = len(track['events'])
            track['events'].sort(key=lambda e: (e.get('audit_at') or '', e.get('audit_log_id') or 0))

            issue_key = S1BugReclaimService._catalog_issue_key(
                int(track['adventurer_id']),
                track.get('target_type') if track.get('target_id') else None,
                track.get('target_id'),
                {
                    'name': track.get('item_name'),
                    'type': track.get('item_type'),
                    'rarity': track.get('item_rarity'),
                },
            )
            status_code, status_label = S1BugReclaimService._resolve_reclaim_status_label(
                issue_key, reclaim_map, eligible_keys, illegal_keys,
            )

            source_label = '申诉补发/新建'
            source_description = None
            source_at = None
            current_qty = None
            if track.get('target_type') in ('warehouse', 'equipment') and track.get('target_id'):
                model = (
                    AdventurerS1WarehouseItem if track['target_type'] == 'warehouse'
                    else AdventurerS1Equipment
                )
                row = (
                    db_manager.query(model)
                    .filter(
                        model.id == int(track['target_id']),
                        model.adventurer_id == int(track['adventurer_id']),
                    )
                    .first()
                )
                if row:
                    current_qty = item_quantity_of(row)
                    aliases = {str(row.name).strip(), S1BugReclaimService._normalize_name(row.name)}
                    gain_log, skipped = S1BugReclaimService._resolve_effective_gain_log(
                        int(track['adventurer_id']),
                        row.name,
                        aliases,
                        getattr(row, 'created_at', None),
                    )
                    if gain_log:
                        source_label = S1BugReclaimService._source_label(gain_log.source_type)
                        source_description = gain_log.description
                        source_at = to_utc_iso(gain_log.created_at) if gain_log.created_at else None

            adv = adv_map.get(int(track['adventurer_id']))
            user = user_map.get(int(track['user_id'])) if track.get('user_id') else None
            change_parts = []
            if track.get('create_count'):
                change_parts.append(f'新增 {track["create_count"]} 次')
            if track.get('qty_increase_steps'):
                change_parts.append(f'加量 {track["qty_increase_steps"]} 次')
            if track.get('qty_decrease_steps'):
                change_parts.append(f'减量 {track["qty_decrease_steps"]} 次')

            items.append({
                'track_key': track['track_key'],
                'track_kind': track['track_kind'],
                'sort_name': S1BugReclaimService._catalog_fuzzy_name(track.get('item_name') or ''),
                'item_name': track.get('item_name'),
                'item_type': track.get('item_type'),
                'item_rarity': track.get('item_rarity'),
                'adventurer_id': int(track['adventurer_id']),
                'adventurer_name': adv.name if adv else '未知',
                'user_id': track.get('user_id'),
                'username': (user or {}).get('username'),
                'nickname': (user or {}).get('nickname'),
                'target_type': track.get('target_type'),
                'target_id': track.get('target_id'),
                'change_kinds': track['change_kinds'],
                'change_summary': ' · '.join(change_parts) if change_parts else '—',
                'appeal_count': track['appeal_count'],
                'appeal_ids': track['appeal_ids'],
                'event_count': track['event_count'],
                'events': track['events'],
                'item_source_label': source_label,
                'item_source_description': source_description,
                'item_source_at': source_at,
                'current_quantity': current_qty,
                'issue_key': issue_key,
                'reclaim_status': status_code,
                'reclaim_status_label': status_label,
            })

        items, filter_meta = S1BugReclaimService._filter_catalog_items_by_fuzzy_appeals(items)

        sort_key = {
            'name': lambda x: (x.get('sort_name') or '', x.get('adventurer_id') or 0),
            'appeals': lambda x: (-int(x.get('appeal_count') or 0), x.get('sort_name') or ''),
            'recent': lambda x: (x['events'][-1]['audit_at'] if x.get('events') else '', x.get('sort_name') or ''),
            'reclaim': lambda x: (
                {'preview_eligible': 0, 'detected_filtered': 1, 'adjusted_qty': 2, 'adjusted_text': 2,
                 'executed': 3, 'executed_text': 3, 'reverted': 4,
                 'dismissed': 5, 'none': 6}.get(x.get('reclaim_status') or 'none', 9),
                x.get('sort_name') or '',
            ),
        }.get(str(sort or 'name'), lambda x: (x.get('sort_name') or '',))
        items.sort(key=sort_key)

        total = len(items)
        shown = items[:limit]
        summary = {
            'total_tracks': total,
            'shown_count': len(shown),
            'limit': limit,
            'sort': sort,
            'audit_rows': len(rows),
            'create_tracks': sum(1 for i in items if 'create_item' in i.get('change_kinds', [])),
            'qty_tracks': sum(1 for i in items if any(k in i.get('change_kinds', []) for k in ('qty_increase', 'qty_decrease'))),
            'preview_eligible': sum(1 for i in items if i.get('reclaim_status') == 'preview_eligible'),
            'reclaim_executed': sum(1 for i in items if str(i.get('reclaim_status', '')).startswith('executed')),
            'skipped_single_appeal': filter_meta.get('skipped_single_appeal', 0),
            'filter': filter_meta,
            'scan': scan_meta,
        }
        return {'items': shown, 'summary': summary}, None

    @staticmethod
    def _revert_capability(row: AdventurerS1BugReclaimItem) -> tuple[bool, str | None]:
        if row.status != 'executed':
            return False, '仅已执行的回收明细可以回溯'
        if row.reverted_at:
            return False, '该条回收已经回溯'

        result = row.result_json if isinstance(row.result_json, dict) else {}
        if result.get('reclaim_mode') == 'text_correction':
            before = result.get('before')
            after = result.get('after')
            if isinstance(before, dict) and isinstance(after, dict) and after.get('id'):
                return True, None
        snapshot = result.get('revert_snapshot')
        if isinstance(snapshot, dict) and int(snapshot.get('version') or 0) >= 1:
            return True, None
        return False, '旧记录未保存精确恢复快照，仅支持查看'

    @staticmethod
    def _history_row_query():
        return (
            db_manager.query(
                AdventurerS1BugReclaimItem,
                AdventurerS1BugReclaimBatch,
                AdventurerS1,
                User,
            )
            .join(
                AdventurerS1BugReclaimBatch,
                AdventurerS1BugReclaimBatch.id == AdventurerS1BugReclaimItem.batch_id,
            )
            .outerjoin(AdventurerS1, AdventurerS1.id == AdventurerS1BugReclaimItem.adventurer_id)
            .outerjoin(User, User.id == AdventurerS1BugReclaimItem.user_id)
        )

    @staticmethod
    def _serialize_history_row(row_tuple, *, detail: bool = False) -> dict:
        row, batch, adventurer, user = row_tuple
        can_revert, unavailable_reason = S1BugReclaimService._revert_capability(row)
        payload = {
            'id': int(row.id),
            'batch_id': int(row.batch_id),
            'batch_status': batch.status,
            'batch_note': batch.note,
            'status': row.status,
            'is_reverted': bool(row.reverted_at),
            'can_revert': can_revert,
            'revert_unavailable_reason': unavailable_reason,
            'issue_key': row.issue_key,
            'issue_type': row.issue_type,
            'appeal_id': row.appeal_id,
            'audit_log_id': row.audit_log_id,
            'adventurer_id': int(row.adventurer_id),
            'adventurer_name': adventurer.name if adventurer else None,
            'user_id': row.user_id,
            'username': user.username if user else None,
            'nickname': user.nickname if user else None,
            'item_name': row.item_name,
            'item_type': row.item_type,
            'item_rarity': row.item_rarity,
            'illegal_quantity': int(row.illegal_quantity or 0),
            'reclaimed_item_quantity': int(row.reclaimed_item_quantity or 0),
            'gold_deducted': round(float(row.gold_deducted or 0), 2),
            'executed_at': to_utc_iso(row.executed_at) if row.executed_at else None,
            'reverted_at': to_utc_iso(row.reverted_at) if row.reverted_at else None,
            'created_at': to_utc_iso(row.created_at) if row.created_at else None,
        }
        if detail:
            payload.update({
                'before': row.before_json or {},
                'after': row.after_json or {},
                'route': row.route_json or [],
                'result': row.result_json or {},
                'revert_result': row.revert_result_json or {},
                'error_message': row.error_message,
                'batch': batch.to_dict(),
            })
        return payload

    @staticmethod
    def history(page: int = 1, per_page: int = 20) -> tuple:
        """逐条分页返回回收明细；列表不携带大 JSON，详情按需读取。"""
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 20), 100))
        q = S1BugReclaimService._history_row_query()
        total = int(
            db_manager.query(func.count(AdventurerS1BugReclaimItem.id)).scalar() or 0
        )
        rows = (
            q.order_by(AdventurerS1BugReclaimItem.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return {
            'items': [S1BugReclaimService._serialize_history_row(row) for row in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': max(1, (total + per_page - 1) // per_page),
        }, None

    @staticmethod
    def history_detail(item_id: int) -> tuple:
        row = (
            S1BugReclaimService._history_row_query()
            .filter(AdventurerS1BugReclaimItem.id == int(item_id))
            .first()
        )
        if row is None:
            return None, '回收历史明细不存在'
        return S1BugReclaimService._serialize_history_row(row, detail=True), None

    @staticmethod
    def _restore_consumed_items(
        history_row: AdventurerS1BugReclaimItem,
        adventurer: AdventurerS1,
        snapshots: list,
    ) -> list[dict]:
        restored = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or int(snapshot.get('quantity') or 0) <= 0:
                raise RuntimeError('恢复快照中的物品数量无效')
            source = str(snapshot.get('source') or '')
            if source not in ('equipment', 'warehouse'):
                raise RuntimeError('恢复快照中的物品位置无效')
            destination, grant_err = grant_raw_item(
                adventurer,
                str(snapshot.get('name') or history_row.item_name),
                {
                    'type': snapshot.get('type') or history_row.item_type,
                    'rarity': snapshot.get('rarity') or history_row.item_rarity,
                    'quantity': int(snapshot.get('quantity') or 0),
                    'uses_left': snapshot.get('uses_left'),
                    'desc': snapshot.get('custom_desc'),
                    'evolution_stage': int(snapshot.get('evolution_stage') or 0),
                },
                source_type='bug_reclaim_revert',
                description=f'管理员回溯异常收益回收明细 #{history_row.id}',
                force_destination=source,
                register_catalog=False,
                related_id=str(history_row.id),
                related_type='bug_reclaim_item',
                allow_same_name_stack_merge=True,
            )
            if grant_err:
                raise RuntimeError(grant_err)
            restored.append({
                'name': snapshot.get('name') or history_row.item_name,
                'quantity': int(snapshot.get('quantity') or 0),
                'requested_destination': source,
                'actual_destination': destination,
            })
        return restored

    @staticmethod
    def _revert_text_correction(row: AdventurerS1BugReclaimItem, result: dict) -> dict:
        before = result.get('before') or {}
        after = result.get('after') or {}
        source = str(after.get('source') or '')
        model = {
            'equipment': AdventurerS1Equipment,
            'warehouse': AdventurerS1WarehouseItem,
        }.get(source)
        if model is None or not after.get('id'):
            raise RuntimeError('旧文字修正记录缺少精确目标快照')
        target = (
            db_manager.query(model)
            .filter(
                model.id == int(after['id']),
                model.adventurer_id == int(row.adventurer_id),
            )
            .with_for_update()
            .first()
        )
        if target is None:
            raise RuntimeError('文字修正后的目标物品已不存在')
        current = {
            'name': target.name,
            'custom_desc': target.custom_desc,
            'quantity': item_quantity_of(target),
        }
        expected = {
            'name': after.get('name'),
            'custom_desc': after.get('custom_desc'),
            'quantity': int(after.get('quantity') or 0),
        }
        if current != expected:
            raise RuntimeError('目标物品在回收后又发生变化，已拒绝覆盖新状态')
        target.name = str(before.get('name') or '')[:100]
        target.custom_desc = before.get('custom_desc')
        target.quantity = int(before.get('quantity') or 1)
        return {'mode': 'text_correction', 'restored_before': before}

    @staticmethod
    def revert_history_item(admin_user_id: int, item_id: int, note: str = '') -> tuple:
        """逐条反向执行回收；旧记录无精确快照时只读，不进行推断恢复。"""
        try:
            with db_manager.transaction():
                row = (
                    db_manager.query(AdventurerS1BugReclaimItem)
                    .filter(AdventurerS1BugReclaimItem.id == int(item_id))
                    .with_for_update()
                    .first()
                )
                if row is None:
                    return None, '回收历史明细不存在'
                can_revert, unavailable_reason = S1BugReclaimService._revert_capability(row)
                if not can_revert:
                    return None, unavailable_reason or '该条回收不可回溯'

                result = row.result_json if isinstance(row.result_json, dict) else {}
                now = _portable_utc_now()
                if result.get('reclaim_mode') == 'text_correction':
                    revert_result = S1BugReclaimService._revert_text_correction(row, result)
                else:
                    snapshot = result.get('revert_snapshot') or {}
                    adventurer = (
                        db_manager.query(AdventurerS1)
                        .filter(AdventurerS1.id == int(row.adventurer_id))
                        .with_for_update()
                        .first()
                    )
                    if adventurer is None:
                        raise RuntimeError('目标冒险者已不存在')
                    restored_items = S1BugReclaimService._restore_consumed_items(
                        row, adventurer, snapshot.get('consumed_items') or [],
                    )
                    mode = str(snapshot.get('mode') or 'default')
                    currency_actions = []
                    if mode == 'market_purchase_undo':
                        buyer_refund = round(float(snapshot.get('buyer_refund_gold') or 0), 2)
                        seller_clawback = round(float(snapshot.get('seller_clawback_gold') or 0), 2)
                        seller_id = int(snapshot.get('seller_adventurer_id') or 0)
                        if buyer_refund > 0:
                            _, err = S1CurrencyService.deduct(
                                int(row.adventurer_id), 'gold', buyer_refund,
                                source_type='bug_reclaim_revert', related_id=row.id,
                                related_type='bug_reclaim_item',
                                description=f'回溯异常收益回收明细 #{row.id}：撤销买家退款',
                                auto_commit=False, allow_negative=True,
                            )
                            if err:
                                raise RuntimeError(err)
                            currency_actions.append({'adventurer_id': row.adventurer_id, 'gold': -buyer_refund})
                        if seller_clawback > 0:
                            if seller_id <= 0:
                                raise RuntimeError('恢复快照缺少卖家冒险者')
                            _, err = S1CurrencyService.add(
                                seller_id, 'gold', seller_clawback,
                                source_type='bug_reclaim_revert', related_id=row.id,
                                related_type='bug_reclaim_item',
                                description=f'回溯异常收益回收明细 #{row.id}：返还卖家扣款',
                                auto_commit=False,
                            )
                            if err:
                                raise RuntimeError(err)
                            currency_actions.append({'adventurer_id': seller_id, 'gold': seller_clawback})
                    else:
                        gold = round(float(snapshot.get('gold_deducted') or 0), 2)
                        if gold > 0:
                            _, err = S1CurrencyService.add(
                                int(row.adventurer_id), 'gold', gold,
                                source_type='bug_reclaim_revert', related_id=row.id,
                                related_type='bug_reclaim_item',
                                description=f'回溯异常收益回收明细 #{row.id}：返还扣款',
                                auto_commit=False,
                            )
                            if err:
                                raise RuntimeError(err)
                            currency_actions.append({'adventurer_id': row.adventurer_id, 'gold': gold})
                    revert_result = {
                        'mode': mode,
                        'restored_items': restored_items,
                        'currency_actions': currency_actions,
                    }

                revert_result['note'] = (note or '').strip()[:200]
                row.reverted_by = int(admin_user_id)
                row.reverted_at = now
                row.revert_result_json = revert_result
        except Exception as exc:
            return None, f'回溯失败：{exc}'
        return S1BugReclaimService.history_detail(item_id)
