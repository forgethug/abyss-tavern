"""深渊赌坊：8 座占位、每分钟一轮集体开注，须赌场荷官在岗；公开下注列表。"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import calendar
import logging
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1CurrencyLog,
    AdventurerS1GamblingBet,
    AdventurerS1GamblingRound,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

logger = logging.getLogger(__name__)

_HANDLER_KEY = 'gambling_hall'
_VALID_CHOICES = frozenset({'high', 'low'})
_RULES_TEXT = (
    '可随时入座赌桌（共 8 位）；须有冒险者在「赌场荷官」岗位打工时方可下注与开奖。'
    '每分钟一轮集体开注，到期统一掷两颗骰子；若开奖时无人当荷官则流局并退还本轮赌注。'
    '点数和 2~6 为小、8~12 为大，7 为庄家通吃；猜中 1:1 奖金。本轮所有下注公开可见。'
)
_UTC_EPOCH = datetime(1970, 1, 1)


class S1GamblingService:
    """深渊赌坊业务逻辑。"""

    @staticmethod
    def _get_linked_work_code() -> str:
        return str(current_app.config.get('ADVENTURER_S1_GAMBLING_LINKED_WORK_CODE', 'casino_dealer'))

    @staticmethod
    def _get_round_seconds() -> int:
        try:
            return max(30, int(current_app.config.get('ADVENTURER_S1_GAMBLING_ROUND_SECONDS', 60)))
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _get_seat_count() -> int:
        try:
            return max(1, int(current_app.config.get('ADVENTURER_S1_GAMBLING_SEAT_COUNT', 8)))
        except (TypeError, ValueError):
            return 8

    @staticmethod
    def _get_min_bet() -> int:
        try:
            return max(1, int(current_app.config.get('ADVENTURER_S1_GAMBLING_MIN_BET', 5)))
        except (TypeError, ValueError):
            return 5

    @staticmethod
    def _get_max_bet() -> int:
        try:
            return max(S1GamblingService._get_min_bet(), int(current_app.config.get('ADVENTURER_S1_GAMBLING_MAX_BET', 100)))
        except (TypeError, ValueError):
            return 100

    @staticmethod
    def _get_tip_tiers() -> list[int]:
        raw = current_app.config.get('ADVENTURER_S1_GAMBLING_TIP_TIERS', [2, 4, 6, 8, 10])
        try:
            tiers = sorted({int(x) for x in raw if int(x) > 0})
        except (TypeError, ValueError):
            tiers = []
        return tiers or [2, 4, 6, 8, 10]

    @staticmethod
    def _get_dealer_share() -> float:
        try:
            share = float(current_app.config.get('ADVENTURER_S1_GAMBLING_DEALER_SHARE', 0.5))
        except (TypeError, ValueError):
            share = 0.5
        return min(1.0, max(0.0, share))

    @staticmethod
    def _active_dealers_query():
        work_code = S1GamblingService._get_linked_work_code()
        return (
            AdventurerS1.query
            .join(
                AdventurerS1BuildingInstance,
                AdventurerS1.building_instance_id == AdventurerS1BuildingInstance.id,
            )
            .join(
                AdventurerS1BuildingTemplate,
                AdventurerS1BuildingInstance.template_id == AdventurerS1BuildingTemplate.id,
            )
            .filter(
                AdventurerS1.status == 'building',
                AdventurerS1BuildingTemplate.code == work_code,
                AdventurerS1BuildingInstance.status == 'built',
                AdventurerS1BuildingInstance.disabled.is_(False),
            )
        )

    @staticmethod
    def count_active_dealers() -> int:
        return S1GamblingService._active_dealers_query().count()

    @staticmethod
    def list_active_dealers() -> list[AdventurerS1]:
        return (
            S1GamblingService._active_dealers_query()
            .order_by(AdventurerS1.building_assigned_at.asc(), AdventurerS1.id.asc())
            .all()
        )

    @staticmethod
    def list_active_dealers_light() -> list[dict]:
        """荷官列表轻量字段，避免 selectin 加载整棵冒险者子表。"""
        rows = (
            S1GamblingService._active_dealers_query()
            .with_entities(
                AdventurerS1.id,
                AdventurerS1.name,
                AdventurerS1.owner_id,
                AdventurerS1.building_usage_extra,
            )
            .order_by(AdventurerS1.building_assigned_at.asc(), AdventurerS1.id.asc())
            .all()
        )
        result = []
        for adv_id, name, owner_id, extra in rows:
            tips = 0
            if isinstance(extra, dict):
                try:
                    tips = max(0, int(extra.get('tips_received') or 0))
                except (TypeError, ValueError):
                    tips = 0
            result.append({
                'id': adv_id,
                'name': name,
                'owner_id': owner_id,
                'tips_received': tips,
            })
        return result

    @staticmethod
    def is_open() -> bool:
        return S1GamblingService.count_active_dealers() > 0

    @staticmethod
    def emit_panel_event(building_instance_id: int, action: str, **extra) -> None:
        """推送赌坊面板刷新事件（前端按 building_instance_id 过滤）。"""
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': action,
                'building_instance_id': building_instance_id,
                **extra,
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def maybe_emit_seat_changed(building_instance_id: int) -> None:
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if inst and inst.template and inst.template.handler_key == _HANDLER_KEY:
            S1GamblingService.emit_panel_event(building_instance_id, 'gambling_seat_changed')

    @staticmethod
    def _utc_now() -> datetime:
        return _portable_utc_now()

    @staticmethod
    def _utc_unix(now: Optional[datetime] = None) -> int:
        """UTC naive datetime → unix 秒（避免 naive.timestamp() 按本地时区解释）。"""
        now = now or S1GamblingService._utc_now()
        return calendar.timegm(now.utctimetuple())

    @staticmethod
    def _round_epoch(now: Optional[datetime] = None) -> int:
        now = now or S1GamblingService._utc_now()
        secs = S1GamblingService._get_round_seconds()
        return S1GamblingService._utc_unix(now) // secs

    @staticmethod
    def _closes_at_for_epoch(epoch: int) -> datetime:
        secs = S1GamblingService._get_round_seconds()
        return _UTC_EPOCH + timedelta(seconds=(epoch + 1) * secs)

    @staticmethod
    def _resolve_outcome(dice_sum: int) -> str:
        if dice_sum <= 6:
            return 'low'
        if dice_sum >= 8:
            return 'high'
        return 'house'

    @staticmethod
    def _bet_wins(outcome: str, choice: str) -> bool:
        """猜中：开奖 high/low 且与下注一致；house(7) 或 cancelled 一律不算中。"""
        return outcome in _VALID_CHOICES and outcome == choice

    @staticmethod
    def _round_ledger_has(round_id: int, adventurer_id: int, source_type: str) -> bool:
        """该轮该冒险者是否已有指定类型的赌坊金币流水（防重复派彩/退款）。"""
        return (
            AdventurerS1CurrencyLog.query.filter_by(
                adventurer_id=adventurer_id,
                currency_code='gold',
                source_type=source_type,
                related_type='gambling_round',
                related_id=str(round_id),
            ).first()
            is not None
        )

    @staticmethod
    def _validate_building(
        inst: Optional[AdventurerS1BuildingInstance],
    ) -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        if not inst:
            return None, '建筑不存在'
        if inst.status != 'built':
            return None, '建筑尚未建成'
        template = inst.template
        if not template or template.handler_key != _HANDLER_KEY:
            return None, '该建筑不是深渊赌坊'
        return inst, None

    @staticmethod
    def _is_seated_at(adv: AdventurerS1, building_instance_id: int) -> bool:
        return (
            adv.status == 'building'
            and adv.building_instance_id == building_instance_id
        )

    @staticmethod
    def _list_seated_adventurers(building_instance_id: int) -> list[AdventurerS1]:
        return (
            AdventurerS1.query.filter_by(
                building_instance_id=building_instance_id,
                status='building',
            )
            .order_by(AdventurerS1.building_assigned_at.asc(), AdventurerS1.id.asc())
            .all()
        )

    @staticmethod
    def _get_adventurer_seat_index(adv: AdventurerS1) -> Optional[int]:
        extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        raw = extra.get('seat_index')
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _occupied_seat_indices(building_instance_id: int) -> dict[int, AdventurerS1]:
        occupied: dict[int, AdventurerS1] = {}
        for adv in S1GamblingService._list_seated_adventurers(building_instance_id):
            idx = S1GamblingService._get_adventurer_seat_index(adv)
            if idx is not None and idx not in occupied:
                occupied[idx] = adv
        return occupied

    @staticmethod
    def validate_seat_for_start(building_instance_id: int, extra_params: Optional[dict]) -> Tuple[Optional[int], Optional[str]]:
        seat_count = S1GamblingService._get_seat_count()
        raw = (extra_params or {}).get('seat_index')
        if raw is None:
            return None, '请选择座位'
        try:
            seat_index = int(raw)
        except (TypeError, ValueError):
            return None, '座位编号无效'
        if seat_index < 0 or seat_index >= seat_count:
            return None, f'座位编号须在 1~{seat_count} 之间'
        if seat_index in S1GamblingService._occupied_seat_indices(building_instance_id):
            return None, '该座位已被占用'
        return seat_index, None

    @staticmethod
    def _serialize_seats(building_instance_id: int, seated: Optional[list] = None) -> list[dict]:
        seat_count = S1GamblingService._get_seat_count()
        seats = [{'index': idx, 'adventurer': None} for idx in range(seat_count)]
        if seated is None:
            seated = S1GamblingService._list_seated_adventurers(building_instance_id)
        unassigned: list[AdventurerS1] = []
        for adv in seated:
            idx = S1GamblingService._get_adventurer_seat_index(adv)
            if idx is not None and 0 <= idx < seat_count and seats[idx]['adventurer'] is None:
                seats[idx]['adventurer'] = {
                    'id': adv.id,
                    'name': adv.name,
                    'owner_id': adv.owner_id,
                }
            else:
                unassigned.append(adv)
        # 兼容旧数据：无 seat_index 的占座者按顺序填入空位（仅展示）
        for adv in unassigned:
            for idx in range(seat_count):
                if seats[idx]['adventurer'] is None:
                    seats[idx]['adventurer'] = {
                        'id': adv.id,
                        'name': adv.name,
                        'owner_id': adv.owner_id,
                    }
                    break
        return seats

    @staticmethod
    def _tips_received_of(adv: AdventurerS1) -> int:
        extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        try:
            return max(0, int(extra.get('tips_received') or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _serialize_dealers(dealers: list[AdventurerS1]) -> list[dict]:
        return [
            {
                'id': d.id,
                'name': d.name,
                'owner_id': d.owner_id,
                'tips_received': S1GamblingService._tips_received_of(d),
            }
            for d in dealers
        ]

    @staticmethod
    def _apply_bet_result_label(bet_item: dict, round_outcome: Optional[str]) -> None:
        if bet_item.get('refunded'):
            return
        if round_outcome == 'house':
            bet_item['result_label'] = '通吃'
            return
        if bet_item.get('won') is True:
            profit = int(bet_item.get('payout') or 0) - int(bet_item.get('bet_amount') or 0)
            bet_item['result_label'] = f'+{profit}'
        elif bet_item.get('won') is False:
            bet_item['result_label'] = f'-{int(bet_item.get("bet_amount") or 0)}'

    @staticmethod
    def _batch_adventurer_names(adventurer_ids: set[int]) -> dict[int, str]:
        if not adventurer_ids:
            return {}
        return dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adventurer_ids))
            .all()
        )

    @staticmethod
    def _bet_to_public_dict(bet: AdventurerS1GamblingBet, name_map: dict[int, str]) -> dict:
        from utils.time_utils import to_utc_iso
        return {
            'adventurer_id': bet.adventurer_id,
            'adventurer_name': name_map.get(bet.adventurer_id),
            'bet_amount': int(bet.bet_amount or 0),
            'choice': bet.choice,
            'choice_label': '大' if bet.choice == 'high' else '小',
            'won': bet.won,
            'payout': int(bet.payout or 0),
            'created_at': to_utc_iso(bet.created_at) if bet.created_at else None,
        }

    @staticmethod
    def _serialize_round(
        round_row: Optional[AdventurerS1GamblingRound],
        my_adventurer_id: Optional[int] = None,
        adv_name_map: Optional[dict[int, str]] = None,
    ) -> Optional[dict]:
        if not round_row:
            return None
        now = S1GamblingService._utc_now()
        seconds_remaining = max(0, int((round_row.closes_at - now).total_seconds()))
        bets = sorted(round_row.bets, key=lambda b: b.created_at or datetime.min)
        if adv_name_map is None:
            adv_name_map = S1GamblingService._batch_adventurer_names(
                {b.adventurer_id for b in bets if b.adventurer_id},
            )
        public_bets = [
            S1GamblingService._bet_to_public_dict(b, adv_name_map) for b in bets
        ]
        payload = {
            'round_id': round_row.id,
            'round_epoch': int(round_row.round_epoch),
            'status': round_row.status,
            'closes_at': round_row.closes_at.isoformat() + 'Z',
            'seconds_remaining': seconds_remaining,
            'bets': public_bets,
            'bet_count': len(public_bets),
            'my_bet': None,
        }
        if round_row.status == 'settled':
            payload.update({
                'dice': [round_row.dice1, round_row.dice2] if round_row.dice1 and round_row.dice2 else None,
                'dice_sum': round_row.dice_sum,
                'outcome': round_row.outcome,
                'outcome_label': {'high': '大', 'low': '小', 'house': '庄家(7)'}.get(round_row.outcome or '', ''),
                'settled_at': round_row.settled_at.isoformat() + 'Z' if round_row.settled_at else None,
            })
            for item in public_bets:
                S1GamblingService._apply_bet_result_label(item, round_row.outcome)
        elif round_row.status == 'cancelled':
            payload.update({
                'dice': None,
                'dice_sum': None,
                'outcome': 'cancelled',
                'outcome_label': '荷官离岗，流局退还赌注',
                'settled_at': round_row.settled_at.isoformat() + 'Z' if round_row.settled_at else None,
            })
            for item in public_bets:
                if item.get('payout', 0) >= item.get('bet_amount', 0):
                    item['refunded'] = True
                    item['result_label'] = '已退还'
        if my_adventurer_id:
            for item in public_bets:
                if item['adventurer_id'] == my_adventurer_id:
                    payload['my_bet'] = item
                    break
        return payload

    @staticmethod
    def settle_due_rounds(building_instance_id: Optional[int] = None, emit: bool = True) -> int:
        """结算已到期的下注轮次（scheduler / 打开赌坊时惰性触发）。

        Args:
            emit: 是否推送 WebSocket 事件。placeBet 内部调用时应传 False 避免事件风暴。
        """
        now = S1GamblingService._utc_now()
        query = AdventurerS1GamblingRound.query.filter(
            AdventurerS1GamblingRound.status == 'betting',
            AdventurerS1GamblingRound.closes_at <= now,
        )
        if building_instance_id is not None:
            query = query.filter_by(building_instance_id=building_instance_id)
        due_rounds = query.order_by(AdventurerS1GamblingRound.closes_at.asc()).all()
        settled = 0
        for rnd in due_rounds:
            ok, err = S1GamblingService._settle_round(rnd.id, emit=emit)
            if ok:
                settled += 1
            elif err:
                logger.warning('赌坊轮次处理失败 round=%s: %s', rnd.id, err)
        return settled

    @staticmethod
    def _lock_round_for_settlement(
        round_id: int,
    ) -> Optional[AdventurerS1GamblingRound]:
        """锁定并刷新轮次，避免复用到期扫描预先载入的旧状态。"""
        return (
            AdventurerS1GamblingRound.query
            .filter_by(id=round_id)
            .populate_existing()
            .with_for_update()
            .first()
        )

    @staticmethod
    def _cancel_round(round_id: int, emit: bool = True) -> Tuple[bool, Optional[str]]:
        """荷官离岗时流局：退还本轮全部赌注，不掷骰。"""
        building_instance_id = None
        affected_adv_ids: list[int] = []
        try:
            with db_manager.transaction():
                rnd = S1GamblingService._lock_round_for_settlement(round_id)
                if not rnd or rnd.status != 'betting':
                    return True, None

                building_instance_id = rnd.building_instance_id
                now = S1GamblingService._utc_now()

                inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, rnd.building_instance_id)
                template_name = inst.template.name if inst and inst.template else '深渊赌坊'

                bets = (
                    AdventurerS1GamblingBet.query.filter_by(round_id=rnd.id)
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                rnd.dice1 = None
                rnd.dice2 = None
                rnd.dice_sum = None
                rnd.outcome = 'cancelled'
                rnd.status = 'cancelled'
                rnd.settled_at = now
                db_manager.flush()

                for bet in bets:
                    refund = int(bet.bet_amount or 0)
                    if refund > 0 and not S1GamblingService._round_ledger_has(
                        rnd.id, bet.adventurer_id, 'gambling_refund',
                    ):
                        # auto_commit=False：退款与轮次置 cancelled 必须同一外层事务原子提交，
                        # 否则中途 commit 释放锁、status 仍 betting，会被重复退款。
                        _, add_err = S1CurrencyService.add(
                            bet.adventurer_id, 'gold', float(refund),
                            source_type='gambling_refund',
                            related_id=rnd.id,
                            related_type='gambling_round',
                            description=f'「{template_name}」第 {rnd.round_epoch} 轮流局退还 {refund} 金',
                            auto_commit=False,
                        )
                        if add_err:
                            raise ValueError(add_err)
                    bet.won = None
                    bet.payout = refund
                    affected_adv_ids.append(bet.adventurer_id)
        except ValueError as exc:
            return False, str(exc)
        except Exception as exc:
            logger.exception('赌坊轮次流局异常 round=%s', round_id)
            return False, player_operation_failure(exc, '操作失败')

        if emit and building_instance_id is not None:
            S1GamblingService.emit_panel_event(
                building_instance_id,
                'gambling_round_cancelled',
                round_id=round_id,
                ids=affected_adv_ids,
            )
        return True, None

    @staticmethod
    def _settle_round(round_id: int, emit: bool = True) -> Tuple[bool, Optional[str]]:
        if not S1GamblingService.is_open():
            return S1GamblingService._cancel_round(round_id, emit=emit)

        building_instance_id = None
        settled_adv_ids: list[int] = []
        try:
            with db_manager.transaction():
                rnd = S1GamblingService._lock_round_for_settlement(round_id)
                if not rnd or rnd.status != 'betting':
                    return True, None

                building_instance_id = rnd.building_instance_id
                dice1 = random.randint(1, 6)
                dice2 = random.randint(1, 6)
                dice_sum = dice1 + dice2
                outcome = S1GamblingService._resolve_outcome(dice_sum)
                now = S1GamblingService._utc_now()

                inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, rnd.building_instance_id)
                template_name = inst.template.name if inst and inst.template else '深渊赌坊'

                # 1) 掷骰并锁定本轮结果（同一事务内与派彩一起 commit）
                rnd.dice1 = dice1
                rnd.dice2 = dice2
                rnd.dice_sum = dice_sum
                rnd.outcome = outcome
                rnd.status = 'settled'
                rnd.settled_at = now
                db_manager.flush()

                # 2) 逐笔结算：猜错/庄家通吃只记 won=False，绝不派彩
                bets = (
                    AdventurerS1GamblingBet.query.filter_by(round_id=rnd.id)
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                if outcome == 'house':
                    # 和为 7：大小皆输，庄家通吃；此分支禁止任何 gambling_win
                    for bet in bets:
                        bet.won = False
                        bet.payout = 0
                        settled_adv_ids.append(bet.adventurer_id)
                else:
                    for bet in bets:
                        won = S1GamblingService._bet_wins(outcome, bet.choice)
                        bet.won = won
                        if won:
                            payout = int(bet.bet_amount) * 2
                            bet.payout = payout
                            if not S1GamblingService._round_ledger_has(
                                rnd.id, bet.adventurer_id, 'gambling_win',
                            ):
                                # auto_commit=False：派彩与轮次置 settled 必须在同一外层事务原子提交。
                                _, add_err = S1CurrencyService.add(
                                    bet.adventurer_id, 'gold', float(payout),
                                    source_type='gambling_win',
                                    related_id=rnd.id,
                                    related_type='gambling_round',
                                    description=f'「{template_name}」第 {rnd.round_epoch} 轮猜中，赢得 {int(bet.bet_amount)} 金',
                                    auto_commit=False,
                                )
                                if add_err:
                                    raise ValueError(add_err)
                        else:
                            bet.payout = 0
                        settled_adv_ids.append(bet.adventurer_id)
        except ValueError as exc:
            return False, str(exc)
        except Exception as exc:
            logger.exception('赌坊轮次结算异常 round=%s', round_id)
            return False, player_operation_failure(exc, '操作失败')

        if emit and building_instance_id is not None:
            S1GamblingService.emit_panel_event(
                building_instance_id,
                'gambling_round_settled',
                round_id=round_id,
                ids=settled_adv_ids,
            )
        return True, None

    @staticmethod
    def _round_is_open(rnd: Optional[AdventurerS1GamblingRound]) -> bool:
        if not rnd or rnd.status != 'betting' or not rnd.closes_at:
            return False
        return (rnd.closes_at - S1GamblingService._utc_now()).total_seconds() > 0

    @staticmethod
    def _resolve_open_round(
        building_instance_id: int,
        *,
        create: bool = True,
    ) -> Optional[AdventurerS1GamblingRound]:
        """读路径：定位当前可下注轮次，不跑多轮 settle 循环（结算由 scheduler / 到期惰性触发）。"""
        now = S1GamblingService._utc_now()
        epoch = S1GamblingService._round_epoch(now)

        def _fetch(ep: int) -> Optional[AdventurerS1GamblingRound]:
            return AdventurerS1GamblingRound.query.filter_by(
                building_instance_id=building_instance_id,
                round_epoch=ep,
            ).first()

        for _ in range(4):
            closes_at = S1GamblingService._closes_at_for_epoch(epoch)
            if closes_at <= now:
                epoch += 1
                continue

            rnd = _fetch(epoch)
            if not rnd and create:
                rnd = AdventurerS1GamblingRound(
                    building_instance_id=building_instance_id,
                    round_epoch=epoch,
                    status='betting',
                    closes_at=closes_at,
                )
                db_manager.add(rnd)
                ok, err = db_manager.commit()
                if not ok:
                    rnd = _fetch(epoch)
                    if not rnd:
                        raise RuntimeError(err or '创建赌坊轮次失败')

            if rnd and rnd.status == 'betting' and S1GamblingService._round_is_open(rnd):
                return rnd

            if rnd and rnd.status in ('settled', 'cancelled'):
                epoch += 1
                continue

            epoch += 1

        return (
            AdventurerS1GamblingRound.query.filter(
                AdventurerS1GamblingRound.building_instance_id == building_instance_id,
                AdventurerS1GamblingRound.status == 'betting',
                AdventurerS1GamblingRound.closes_at > now,
            )
            .order_by(AdventurerS1GamblingRound.closes_at.asc())
            .first()
        )

    @staticmethod
    def _ensure_current_round(building_instance_id: int, emit: bool = True) -> AdventurerS1GamblingRound:
        """返回当前可下注轮次（status=betting 且未封盘）。已结算轮次不会作为 current 返回。"""
        S1GamblingService.settle_due_rounds(building_instance_id, emit=emit)
        now = S1GamblingService._utc_now()
        epoch = S1GamblingService._round_epoch(now)

        def _fetch(ep: int) -> Optional[AdventurerS1GamblingRound]:
            return AdventurerS1GamblingRound.query.filter_by(
                building_instance_id=building_instance_id,
                round_epoch=ep,
            ).first()

        for _ in range(64):
            closes_at = S1GamblingService._closes_at_for_epoch(epoch)
            if closes_at <= now:
                epoch += 1
                continue

            rnd = _fetch(epoch)
            if not rnd:
                rnd = AdventurerS1GamblingRound(
                    building_instance_id=building_instance_id,
                    round_epoch=epoch,
                    status='betting',
                    closes_at=closes_at,
                )
                db_manager.add(rnd)
                ok, err = db_manager.commit()
                if not ok:
                    existing = _fetch(epoch)
                    if existing:
                        rnd = existing
                    else:
                        raise RuntimeError(err or '创建赌坊轮次失败')

            if rnd.status == 'betting':
                if S1GamblingService._round_is_open(rnd):
                    return rnd
                S1GamblingService._settle_round(rnd.id, emit=emit)
                epoch += 1
                continue

            if rnd.status in ('settled', 'cancelled'):
                epoch += 1
                continue

            epoch += 1

        raise RuntimeError('无法定位赌坊当前轮次')

    @staticmethod
    def _get_user_adventurer(user_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        from sqlalchemy.orm import joinedload, noload

        adv = (
            AdventurerS1.query
            .options(
                joinedload(AdventurerS1.building_instance),
                noload(AdventurerS1.traits),
                noload(AdventurerS1.equipment_items),
                noload(AdventurerS1.skill_items),
                noload(AdventurerS1.investor_items),
                noload(AdventurerS1.warehouse_items),
                noload(AdventurerS1.dorm_room),
            )
            .filter_by(owner_id=user_id)
            .filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
            .order_by(AdventurerS1.id.asc())
            .first()
        )
        if not adv:
            return None, '你还没有冒险者，请先创建'
        return adv, None

    @staticmethod
    def get_status(
        building_instance_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        from sqlalchemy.orm import joinedload

        inst = (
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id, options=(joinedload(AdventurerS1BuildingInstance.template),))
        )
        inst, err = S1GamblingService._validate_building(inst)
        if err:
            return None, err

        adv, _ = S1GamblingService._get_user_adventurer(user_id)
        my_adv_id = adv.id if adv else None
        gold_balance = 0
        adv_summary = None
        is_seated = False

        if adv:
            balance, bal_err = S1CurrencyService.get_balance(adv.id, 'gold')
            if bal_err:
                return None, bal_err
            gold_balance = int(balance or 0)
            is_seated = S1GamblingService._is_seated_at(adv, building_instance_id)
            can_sit, dispatch_reason = adv.can_dispatch()
            reject_reason = None
            if not can_sit and not is_seated:
                reject_reason = dispatch_reason
            adv_summary = {
                'id': adv.id,
                'name': adv.name,
                'gold': gold_balance,
                'is_seated': is_seated,
                'can_sit': can_sit or is_seated,
                'reject_reason': reject_reason,
            }

        now = S1GamblingService._utc_now()
        has_due = (
            AdventurerS1GamblingRound.query.filter(
                AdventurerS1GamblingRound.building_instance_id == building_instance_id,
                AdventurerS1GamblingRound.status == 'betting',
                AdventurerS1GamblingRound.closes_at <= now,
            ).limit(1).first() is not None
        )
        if has_due:
            S1GamblingService.settle_due_rounds(building_instance_id, emit=False)

        current_round = S1GamblingService._resolve_open_round(building_instance_id)
        finished_rounds = (
            AdventurerS1GamblingRound.query.filter(
                AdventurerS1GamblingRound.building_instance_id == building_instance_id,
                AdventurerS1GamblingRound.status.in_(('settled', 'cancelled')),
            )
            .order_by(AdventurerS1GamblingRound.round_epoch.desc())
            .limit(16)
            .all()
        )
        last_finished = next((r for r in finished_rounds if r.bets), None)
        if last_finished is None and finished_rounds:
            last_finished = finished_rounds[0]

        bet_adv_ids: set[int] = set()
        for rnd in (current_round, last_finished):
            if rnd:
                bet_adv_ids.update(b.adventurer_id for b in rnd.bets if b.adventurer_id)
        adv_name_map = S1GamblingService._batch_adventurer_names(bet_adv_ids)

        dealers = S1GamblingService.list_active_dealers_light()
        dealer_count = len(dealers)
        template = inst.template
        seated = S1GamblingService._list_seated_adventurers(building_instance_id)

        return {
            'building_id': inst.id,
            'hall_name': template.name if template else '深渊赌坊',
            'is_open': dealer_count > 0,
            'active_dealers': dealer_count,
            'dealers': dealers,
            'tip_tiers': S1GamblingService._get_tip_tiers(),
            'linked_work_name': '赌场荷官',
            'seat_count': S1GamblingService._get_seat_count(),
            'seated_count': len(seated),
            'seats': S1GamblingService._serialize_seats(building_instance_id, seated=seated),
            'min_bet': S1GamblingService._get_min_bet(),
            'max_bet': S1GamblingService._get_max_bet(),
            'round_seconds': S1GamblingService._get_round_seconds(),
            'adventurer': adv_summary,
            'current_round': S1GamblingService._serialize_round(
                current_round, my_adv_id, adv_name_map=adv_name_map,
            ),
            'last_round': S1GamblingService._serialize_round(
                last_finished, my_adv_id, adv_name_map=adv_name_map,
            ),
            'rules': _RULES_TEXT,
        }, None

    @staticmethod
    def place_bet(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        bet_amount: int,
        choice: str,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        inst, err = S1GamblingService._validate_building(
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id),
        )
        if err:
            return None, err

        if not S1GamblingService.is_open():
            return None, '赌坊未开市：需要至少一名冒险者在「赌场荷官」岗位打工'

        choice = (choice or '').strip().lower()
        if choice not in _VALID_CHOICES:
            return None, 'choice 必须是 high 或 low'

        try:
            bet = int(bet_amount)
        except (TypeError, ValueError):
            return None, 'bet_amount 必须是正整数'
        min_bet = S1GamblingService._get_min_bet()
        max_bet = S1GamblingService._get_max_bet()
        if bet < min_bet or bet > max_bet:
            return None, f'赌注须在 {min_bet}~{max_bet} 金币之间'

        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and adv.owner_id != user_id:
            return None, '只能让自己的冒险者下注'
        if adv.status == 'exploring':
            return None, '探险中无法进行此操作'
        if not S1GamblingService._is_seated_at(adv, building_instance_id):
            return None, '须先入座赌桌才能下注'

        balance, bal_err = S1CurrencyService.get_balance(adventurer_id, 'gold')
        if bal_err:
            return None, bal_err
        if (balance or 0) < bet:
            return None, f'金币不足（需要 {bet}，当前 {int(balance or 0)}）'

        S1GamblingService.settle_due_rounds(building_instance_id, emit=False)
        current_round = S1GamblingService._ensure_current_round(building_instance_id, emit=False)
        if not S1GamblingService._round_is_open(current_round):
            return None, '本轮已封盘，请等待下一轮'

        existing = AdventurerS1GamblingBet.query.filter_by(
            round_id=current_round.id,
            adventurer_id=adventurer_id,
        ).first()
        if existing:
            return None, '本轮你已下注，请等待开骰'

        template_name = inst.template.name if inst.template else '深渊赌坊'
        choice_label = '大' if choice == 'high' else '小'

        try:
            with db_manager.transaction():
                rnd = AdventurerS1GamblingRound.query.session.get(AdventurerS1GamblingRound, current_round.id, with_for_update=True)
                if not rnd or not S1GamblingService._round_is_open(rnd):
                    raise ValueError('本轮已封盘，请等待下一轮')

                dup = AdventurerS1GamblingBet.query.filter_by(
                    round_id=rnd.id,
                    adventurer_id=adventurer_id,
                ).first()
                if dup:
                    raise ValueError('本轮你已下注，请等待开骰')

                # auto_commit=False：扣注与下注记录写入须同一外层事务原子提交，
                # 否则中途 commit 释放锁、提前落账，扣款与 bet 行可能不一致。
                _, deduct_err = S1CurrencyService.deduct(
                    adventurer_id, 'gold', float(bet),
                    source_type='gambling_bet',
                    related_id=rnd.id,
                    related_type='gambling_round',
                    description=f'「{template_name}」第 {rnd.round_epoch} 轮下注 {bet} 金（猜{choice_label}）',
                    auto_commit=False,
                )
                if deduct_err:
                    raise ValueError(deduct_err)

                bet_row = AdventurerS1GamblingBet(
                    round_id=rnd.id,
                    adventurer_id=adventurer_id,
                    bet_amount=bet,
                    choice=choice,
                )
                db_manager.add(bet_row)

                new_balance, _ = S1CurrencyService.get_balance(adventurer_id, 'gold')
        except ValueError as exc:
            return None, str(exc)
        except Exception:
            return None, '下注失败，请稍后重试'

        status, _ = S1GamblingService.get_status(building_instance_id, user_id, is_admin=is_admin)
        bet_public = AdventurerS1GamblingBet.query.filter_by(
            round_id=current_round.id,
            adventurer_id=adventurer_id,
        ).first()
        S1GamblingService.emit_panel_event(
            building_instance_id,
            'gambling_bet_placed',
            round_id=current_round.id,
        )
        return {
            'bet': bet_public.to_public_dict() if bet_public else None,
            'gold': int(new_balance or 0),
            'status': status,
        }, None

    @staticmethod
    def tip_dealer(
        building_instance_id: int,
        tipper_adventurer_id: int,
        dealer_adventurer_id: int,
        user_id: int,
        amount: int,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """打赏在岗荷官：扣打赏者全额，荷官实收一半（向下取整），余额蒸发。"""
        inst, err = S1GamblingService._validate_building(
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id),
        )
        if err:
            return None, err

        if not S1GamblingService.is_open():
            return None, '赌坊未开市：当前没有荷官在岗'

        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return None, '打赏金额无效'
        tiers = S1GamblingService._get_tip_tiers()
        if amount not in tiers:
            return None, '打赏档位须是 ' + '/'.join(str(t) for t in tiers) + ' 金之一'

        tipper = AdventurerS1.query.session.get(AdventurerS1, tipper_adventurer_id)
        if not tipper:
            return None, '冒险者不存在'
        if not is_admin and tipper.owner_id != user_id:
            return None, '只能用自己的冒险者打赏'
        if tipper.status == 'exploring':
            return None, '探险中无法进行此操作'
        if not S1GamblingService._is_seated_at(tipper, building_instance_id):
            return None, '须先入座赌桌才能打赏荷官'

        dealer = AdventurerS1.query.session.get(AdventurerS1, dealer_adventurer_id)
        if not dealer:
            return None, '荷官不存在'
        if dealer.id == tipper.id or dealer.owner_id == tipper.owner_id:
            return None, '不能打赏自己的荷官'
        if dealer.id not in {d.id for d in S1GamblingService.list_active_dealers()}:
            return None, '该荷官已离岗'

        dealer_gain = int(amount * S1GamblingService._get_dealer_share())
        if dealer_gain <= 0:
            return None, '打赏金额过低'

        balance, bal_err = S1CurrencyService.get_balance(tipper.id, 'gold')
        if bal_err:
            return None, bal_err
        if (balance or 0) < amount:
            return None, f'金币不足（需要 {amount}，当前 {int(balance or 0)}）'

        template_name = inst.template.name if inst.template else '深渊赌坊'
        try:
            with db_manager.transaction():
                # 锁荷官行并复核仍在岗，再扣款/入账/累计，全程同一事务原子提交
                locked_dealer = AdventurerS1.query.session.get(AdventurerS1, dealer.id, with_for_update=True)
                if not locked_dealer or locked_dealer.status != 'building':
                    raise ValueError('该荷官已离岗')

                _, deduct_err = S1CurrencyService.deduct(
                    tipper.id, 'gold', float(amount),
                    source_type='gambling_tip',
                    related_id=dealer.id,
                    related_type='gambling_tip',
                    description=f'在「{template_name}」打赏荷官 {dealer.name} {amount} 金',
                    auto_commit=False,
                )
                if deduct_err:
                    raise ValueError(deduct_err)

                _, add_err = S1CurrencyService.add(
                    dealer.id, 'gold', float(dealer_gain),
                    source_type='gambling_tip',
                    related_id=tipper.id,
                    related_type='gambling_tip',
                    description=f'收到 {tipper.name} 在「{template_name}」的打赏 {dealer_gain} 金',
                    auto_commit=False,
                )
                if add_err:
                    raise ValueError(add_err)

                # JSON 字段就地改不触发脏检测，整体重新赋值
                extra = dict(locked_dealer.building_usage_extra) if isinstance(locked_dealer.building_usage_extra, dict) else {}
                extra['tips_received'] = max(0, int(extra.get('tips_received') or 0)) + dealer_gain
                locked_dealer.building_usage_extra = extra

                new_balance, _ = S1CurrencyService.get_balance(tipper.id, 'gold')
        except ValueError as exc:
            return None, str(exc)
        except Exception:
            logger.exception('赌坊打赏异常 dealer=%s tipper=%s', dealer_adventurer_id, tipper_adventurer_id)
            return None, '打赏失败，请稍后重试'

        S1GamblingService.emit_panel_event(
            building_instance_id,
            'gambling_tip',
            dealer_id=dealer.id,
            ids=[tipper.id, dealer.id],
        )
        status, _ = S1GamblingService.get_status(building_instance_id, user_id, is_admin=is_admin)
        return {
            'dealer_id': dealer.id,
            'dealer_name': dealer.name,
            'amount': amount,
            'dealer_gain': dealer_gain,
            'gold': int(new_balance or 0),
            'status': status,
        }, None


def settle_due_rounds_job() -> int:
    """APScheduler 入口：结算所有到期赌坊轮次。"""
    return S1GamblingService.settle_due_rounds()
