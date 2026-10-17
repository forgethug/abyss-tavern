"""深渊酒馆 S1 货币管理统一接口。

所有冒险者货币变动（酒馆币、金币等）必须通过此接口，
禁止直接修改 AdventurerS1.gold 等字段。

使用方式：
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

    # 查询余额
    balance, err = S1CurrencyService.get_balance(adventurer_id, 'tavern_coin')

    # 增加
    new_bal, err = S1CurrencyService.add(adventurer_id, 'tavern_coin', 50.0,
                                          source_type='expedition_reward',
                                          description='探险结算奖励')

    # 扣除
    new_bal, err = S1CurrencyService.deduct(adventurer_id, 'tavern_coin', 20.0,
                                             source_type='shop_buy',
                                             description='购买草药')
"""

from plugins.adventurer_s1.services.s1_player_error_service import (
    S1PlayerVisibleError, player_database_result, player_operation_failure,
)
import logging
import math
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy.orm import lazyload
from db_repository import db_manager, DatabaseError, ConcurrencyError
from db_repository.decorators import db_retry
from plugins.adventurer_s1.models import (
    AdventurerS1Currency, AdventurerS1CurrencyLog, AdventurerS1,
)

logger = logging.getLogger(__name__)


class S1CurrencyService:
    """深渊酒馆 S1 货币管理统一接口。所有货币变动必须走此接口。"""

    LOW_CONSUMPTION_CURRENCY_CODE = 'low_consumption_gold'
    LOW_CONSUMPTION_INCOME_SOURCE_LABELS = {
        'market_sell': '个人市场售出',
        'market_buy_order_sale': '响应市场求购售出',
        'expedition_reward': '探险结算',
        'hidden_boss_clue_redeem': '隐藏 Boss 线索兑换',
        'data_appeal_ai': '数据申诉补发',
        'data_appeal_team_pool': '随队探险补偿',
        's1_tavern_relief': '酒馆补贴申请',
        'data_appeal_tavern_subsidy': '申诉酒馆补贴',
        'task_reward': '任务奖励',
    }

    @staticmethod
    def _reserved_low_consumption_error(currency_code: str) -> Optional[str]:
        if str(currency_code or '').strip() == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE:
            return '低消费临时金只能由系统累积并用于白名单设施'
        return None

    # ── 内部工具 ────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_related_id(related_id) -> Optional[str]:
        """CurrencyLog.related_id 为 VARCHAR；PostgreSQL 禁止与 integer 直接比较。"""
        if related_id is None:
            return None
        return str(related_id)

    @staticmethod
    def _validate_amount(amount) -> Tuple[Optional[float], Optional[str]]:
        if not isinstance(amount, (int, float)):
            return None, '金额必须为数字'
        if isinstance(amount, bool):
            return None, '金额不能是布尔值'
        if math.isnan(amount):
            return None, '金额不能是 NaN'
        if math.isinf(amount):
            return None, '金额不能是无穷大'
        if abs(amount) > 1e12:
            return None, '金额超出允许范围'
        return round(float(amount), 2), None

    @staticmethod
    def _get_or_create(adventurer_id: int, currency_code: str, for_update: bool = False) -> Optional[AdventurerS1Currency]:
        """获取货币记录，不存在则返回 None（add 方法会自动创建）。"""
        query = AdventurerS1Currency.query.filter_by(
            adventurer_id=adventurer_id, currency_code=currency_code,
        )
        if for_update:
            query = query.populate_existing().with_for_update()
        return query.first()

    @staticmethod
    def _lock_adventurers(adventurer_ids) -> dict[int, AdventurerS1]:
        """先锁主表，再锁货币表，统一 gold 双写的跨服务锁序。"""
        ids = sorted({int(value) for value in adventurer_ids if int(value) > 0})
        if not ids:
            return {}
        rows = (
            AdventurerS1.query
            # 货币锁只用主表状态；不在持锁期间预加载特质、装备、技能和宿舍住客。
            # 保留按需加载，调用方随后读取真实关系时不能拿到 noload 的空集合。
            .options(lazyload('*'))
            .filter(AdventurerS1.id.in_(ids))
            .order_by(AdventurerS1.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        return {int(row.id): row for row in rows}

    @staticmethod
    def _log(adventurer_id: int, currency_code: str, change_amount: float,
             balance_after: float, source_type: str,
             related_id=None, related_type=None, description=None,
             silent: bool = False):
        """写入变动日志。必须在事务内调用。silent=True 时跳过（GM 隐身）。"""
        log = S1CurrencyService._build_log(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
            change_amount=change_amount,
            balance_after=balance_after,
            source_type=source_type,
            related_id=related_id,
            related_type=related_type,
            description=description,
            silent=silent,
        )
        if log is not None:
            db_manager.add(log)
        return log

    @staticmethod
    def _deduct_once(
        adventurer_id: int,
        currency_code: str,
        amount: float,
        source_type: str,
        related_id,
        related_type,
        description,
        auto_commit: bool,
        allow_negative: bool = False,
        silent: bool = False,
        raise_db_errors: bool = False,
    ):
        """执行一次扣款，不重试；调用方可据此把更大的业务事务作为原子边界。"""
        def fail(error: str):
            if auto_commit:
                db_manager.rollback()
            return None, error, None

        reserved_err = S1CurrencyService._reserved_low_consumption_error(currency_code)
        if reserved_err:
            return fail(reserved_err)
        validated, err = S1CurrencyService._validate_amount(amount)
        if err:
            return fail(err)
        amount = validated
        if amount <= 0:
            return fail('扣减金额必须大于0')

        S1CurrencyService._lock_adventurers([adventurer_id])
        rec = AdventurerS1Currency.query.filter_by(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
        ).populate_existing().with_for_update().first()

        if rec is None:
            # gold 双轨兜底：记录缺失时按主表余额起账，避免历史漂移期误判余额不足
            initial = S1CurrencyService._initial_amount_for(adventurer_id, currency_code)
            if not allow_negative and initial < amount - 0.001:
                return fail(f'{currency_code} 余额不足')
            rec = AdventurerS1Currency(
                adventurer_id=adventurer_id,
                currency_code=currency_code,
                amount=initial,
            )
            if raise_db_errors:
                from extensions import db

                db.session.add(rec)
                db.session.flush()
            else:
                ok, err = player_database_result(db_manager.add(rec))
                if not ok:
                    return fail(err)
        elif not allow_negative and float(rec.amount) < amount - 0.001:
            return fail(f'{currency_code} 余额不足')

        rec.amount = round(float(rec.amount) - amount, 2)
        if currency_code == 'gold':
            S1CurrencyService._sync_gold_to_main(adventurer_id, rec.amount)

        log = S1CurrencyService._build_log(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
            change_amount=-amount,
            balance_after=rec.amount,
            source_type=source_type,
            related_id=related_id,
            related_type=related_type,
            description=description,
            silent=silent,
        )
        if log is not None:
            if raise_db_errors:
                from extensions import db

                db.session.add(log)
                db.session.flush()
            else:
                ok, log_err = player_database_result(db_manager.add(log))
                if not ok:
                    return fail(log_err)

        if auto_commit:
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err, None
        return round(float(rec.amount), 2), None, log

    @staticmethod
    def _build_log(adventurer_id: int, currency_code: str, change_amount: float,
                   balance_after: float, source_type: str,
                   related_id=None, related_type=None, description=None,
                   silent: bool = False,
                   business_payload: Optional[dict] = None,
                   business_expires_at: Optional[datetime] = None) -> Optional[AdventurerS1CurrencyLog]:
        """构造变动日志；批量货币操作可集中 add_all，避免逐条 flush。"""
        if silent:
            return None
        # VARCHAR(200) 防截断：AI 生成的 reason 可能超长
        if description and len(description) > 200:
            description = description[:197] + '...'
        return AdventurerS1CurrencyLog(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
            change_amount=round(change_amount, 2),
            balance_after=round(balance_after, 2),
            source_type=source_type,
            related_id=S1CurrencyService.normalize_related_id(related_id),
            related_type=related_type,
            description=description,
            business_payload=(
                dict(business_payload) if isinstance(business_payload, dict) else None
            ),
            business_expires_at=business_expires_at,
        )

    @staticmethod
    def _sync_gold_to_main(adventurer_id: int, new_amount: float) -> None:
        """gold 是双轨字段：Currency 表为账本权威，主表 `AdventurerS1.gold` 为展示/AI prompt
        副本。任何 gold 变动后必须调用本方法同步主表（2026-06-10 并轨修复）。

        用 query.get 走 identity map：若主表对象已在当前 session 加载，直接改属性保证
        调用方后续 to_dict 读到新值。
        """
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, options=(lazyload('*'),))
        if adv is not None:
            adv.gold = round(float(new_amount), 2)

    @staticmethod
    def _initial_amount_for(adventurer_id: int, currency_code: str) -> float:
        """新建 Currency 记录的初始余额：gold 以主表当前值为准（双轨兜底，防历史
        漂移期缺记录时把余额误归零），其他货币从 0 起。"""
        if currency_code == 'gold':
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, options=(lazyload('*'),))
            if adv is not None:
                return round(float(adv.gold or 0), 2)
        return 0.0

    @staticmethod
    def _low_consumption_enabled() -> bool:
        return bool(current_app.config.get(
            'ADVENTURER_S1_LOW_CONSUMPTION_ENABLED', True,
        ))

    @staticmethod
    def low_consumption_cap() -> float:
        try:
            cap = float(current_app.config.get(
                'ADVENTURER_S1_LOW_CONSUMPTION_CAP', 100.0,
            ))
        except (TypeError, ValueError):
            cap = 100.0
        if not math.isfinite(cap):
            cap = 100.0
        return round(max(0.0, cap), 2)

    @staticmethod
    def _low_consumption_rate() -> float:
        try:
            rate = float(current_app.config.get(
                'ADVENTURER_S1_LOW_CONSUMPTION_INCOME_RATE', 0.2,
            ))
        except (TypeError, ValueError):
            rate = 0.2
        if not math.isfinite(rate):
            rate = 0.2
        return max(0.0, min(1.0, rate))

    @staticmethod
    def is_low_consumption_facility(facility_key: str) -> bool:
        if not S1CurrencyService._low_consumption_enabled():
            return False
        whitelist = current_app.config.get(
            'ADVENTURER_S1_LOW_CONSUMPTION_FACILITY_WHITELIST',
            ('sanatorium', 'hospital', 'clinic', 'tea_house'),
        )
        return str(facility_key or '').strip() in {
            str(value).strip() for value in (whitelist or ()) if str(value).strip()
        }

    @staticmethod
    def _is_low_consumption_income(source_type: str) -> bool:
        if not S1CurrencyService._low_consumption_enabled():
            return False
        source_types = current_app.config.get(
            'ADVENTURER_S1_LOW_CONSUMPTION_INCOME_SOURCE_TYPES', (),
        )
        return str(source_type or '').strip() in {
            str(value).strip() for value in (source_types or ()) if str(value).strip()
        }

    @staticmethod
    def _plan_low_consumption_split(
        entries_by_adventurer: dict[int, list[dict]],
        starting_gold_balances: dict[int, float],
        *,
        amount_key: str,
    ) -> tuple[dict[int, list[dict]], dict]:
        """锁定临时金并规划负债收入分流，不在本阶段修改任何余额。

        合格收入发生前金币为负时，收入中的配置比例从金币入账额分流到临时金；
        临时金封顶后无法分流的部分仍进入金币。调用方随后必须先完成余额校验，再调用
        ``_apply_low_consumption_split`` 落账，保证金币与临时金之和严格等于原始净收益。
        """
        adjusted = {
            int(adventurer_id): [dict(entry) for entry in entries]
            for adventurer_id, entries in entries_by_adventurer.items()
        }
        rate = S1CurrencyService._low_consumption_rate()
        cap = S1CurrencyService.low_consumption_cap()
        if rate <= 0 or cap <= 0:
            return adjusted, {'row_map': {}, 'final_balances': {}, 'credits': {}}

        adventurer_ids = sorted(
            adventurer_id
            for adventurer_id, entries in adjusted.items()
            if float(starting_gold_balances[adventurer_id]) < -0.001
            and any(
                round(float(entry.get(amount_key) or 0), 2) > 0
                and S1CurrencyService._is_low_consumption_income(entry.get('source_type'))
                for entry in entries
            )
        )
        if not adventurer_ids:
            return adjusted, {'row_map': {}, 'final_balances': {}, 'credits': {}}
        rows = (
            AdventurerS1Currency.query
            .filter(
                AdventurerS1Currency.adventurer_id.in_(adventurer_ids),
                AdventurerS1Currency.currency_code
                == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
            )
            .order_by(AdventurerS1Currency.adventurer_id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        row_map = {int(row.adventurer_id): row for row in rows}
        final_balances: dict[int, float] = {}
        credits_by_adventurer: dict[int, list[dict]] = {}
        for adventurer_id in adventurer_ids:
            row = row_map.get(adventurer_id)
            running_temporary = min(
                cap,
                max(0.0, round(float(row.amount or 0), 2) if row is not None else 0.0),
            )
            running_gold = round(float(starting_gold_balances[adventurer_id]), 2)
            credits = []
            for entry in adjusted[adventurer_id]:
                original_amount = round(float(entry.get(amount_key) or 0), 2)
                source_type = str(entry.get('source_type') or '').strip()
                credited = 0.0
                if (
                    original_amount > 0
                    and running_gold < -0.001
                    and S1CurrencyService._is_low_consumption_income(source_type)
                ):
                    requested = round(original_amount * rate, 2)
                    credited = round(
                        min(requested, max(0.0, cap - running_temporary)),
                        2,
                    )
                if credited <= 0:
                    running_gold = round(running_gold + original_amount, 2)
                    continue
                effective_gold = round(original_amount - credited, 2)
                entry[amount_key] = effective_gold
                running_gold = round(running_gold + effective_gold, 2)
                running_temporary = round(running_temporary + credited, 2)
                credits.append({
                    'amount': credited,
                    'balance_after': running_temporary,
                    'source_type': source_type,
                    'related_id': entry.get('related_id'),
                    'related_type': entry.get('related_type'),
                    'silent': bool(entry.get('silent', False)),
                })
            if credits:
                final_balances[adventurer_id] = running_temporary
                credits_by_adventurer[adventurer_id] = credits

        return adjusted, {
            'row_map': row_map,
            'final_balances': final_balances,
            'credits': credits_by_adventurer,
            'rate_percent': rate * 100,
        }

    @staticmethod
    def _apply_low_consumption_split(plan: dict) -> None:
        """落账已通过调用方余额校验的临时金分流计划。"""
        row_map = plan.get('row_map') or {}
        final_balances = plan.get('final_balances') or {}
        credits_by_adventurer = plan.get('credits') or {}
        rate_percent = float(plan.get('rate_percent') or 0)
        logs = []
        for adventurer_id in sorted(credits_by_adventurer):
            row = row_map.get(adventurer_id)
            if row is None:
                row = AdventurerS1Currency(
                    adventurer_id=adventurer_id,
                    currency_code=S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                    amount=0.0,
                )
                ok, err = db_manager.add(row, flush=False)
                if not ok:
                    raise RuntimeError(err)
            row.amount = round(float(final_balances[adventurer_id]), 2)
            for credit in credits_by_adventurer[adventurer_id]:
                source_label = S1CurrencyService.LOW_CONSUMPTION_INCOME_SOURCE_LABELS.get(
                    credit['source_type'],
                    '合格收益',
                )
                log = S1CurrencyService._build_log(
                    adventurer_id=adventurer_id,
                    currency_code=S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                    change_amount=credit['amount'],
                    balance_after=credit['balance_after'],
                    source_type='low_consumption_split',
                    related_id=credit.get('related_id'),
                    related_type=credit.get('related_type'),
                    description=(
                        f'负债期间将{source_label}净收益的 {rate_percent:g}% '
                        '划入低消费临时金'
                    ),
                    silent=credit['silent'],
                )
                if log is not None:
                    logs.append(log)
        if logs:
            ok, err = db_manager.add_all(logs, flush=False)
            if not ok:
                raise RuntimeError(err)

    @staticmethod
    def get_low_consumption_payment_balances(
        adventurer_id: int,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """只读返回普通金币、临时金及白名单可支付总额。"""
        try:
            records = (
                AdventurerS1Currency.query
                .filter(
                    AdventurerS1Currency.adventurer_id == adventurer_id,
                    AdventurerS1Currency.currency_code.in_([
                        'gold', S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                    ]),
                )
                .all()
            )
            by_code = {row.currency_code: round(float(row.amount or 0), 2) for row in records}
            gold = by_code.get(
                'gold', S1CurrencyService._initial_amount_for(adventurer_id, 'gold'),
            )
            temporary = min(
                S1CurrencyService.low_consumption_cap(),
                max(0.0, by_code.get(S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE, 0.0)),
            )
            return {
                'gold_balance': round(float(gold), 2),
                'low_consumption_balance': round(float(temporary), 2),
                'available': round(max(0.0, float(gold)) + float(temporary), 2),
                'cap': S1CurrencyService.low_consumption_cap(),
            }, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def _deduct_low_consumption_once(
        adventurer_id: int,
        amount: float,
        facility_key: str,
        source_type: str,
        related_id,
        related_type,
        description,
        auto_commit: bool,
    ) -> Tuple[Optional[dict], Optional[str]]:
        def fail(error: str):
            if auto_commit:
                db_manager.rollback()
            return None, error

        validated, err = S1CurrencyService._validate_amount(amount)
        if err:
            return fail(err)
        amount = validated
        if amount <= 0:
            return fail('扣减金额必须大于0')
        if not S1CurrencyService.is_low_consumption_facility(facility_key):
            return fail('该设施不在低消费临时金白名单中')

        locked_adv = S1CurrencyService._lock_adventurers([adventurer_id]).get(
            int(adventurer_id),
        )
        if locked_adv is None:
            return fail('冒险者不存在')
        rows = (
            AdventurerS1Currency.query
            .filter(
                AdventurerS1Currency.adventurer_id == adventurer_id,
                AdventurerS1Currency.currency_code.in_([
                    'gold', S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                ]),
            )
            .order_by(AdventurerS1Currency.currency_code.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        row_map = {row.currency_code: row for row in rows}
        gold_balance = round(float(
            row_map['gold'].amount
            if 'gold' in row_map
            else S1CurrencyService._initial_amount_for(adventurer_id, 'gold')
        ), 2)
        temporary_balance = min(
            S1CurrencyService.low_consumption_cap(),
            max(0.0, round(float(
                row_map[S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE].amount
                if S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE in row_map
                else 0.0
            ), 2)),
        )
        gold_spent = round(min(max(0.0, gold_balance), amount), 2)
        temporary_spent = round(amount - gold_spent, 2)
        if temporary_spent > temporary_balance + 0.001:
            available = round(max(0.0, gold_balance) + temporary_balance, 2)
            return fail(f'可用金额不足（需要 {amount:g}，金币与临时金合计 {available:g}）')

        gold_row = row_map.get('gold')
        if gold_row is None:
            gold_row = AdventurerS1Currency(
                adventurer_id=adventurer_id,
                currency_code='gold',
                amount=gold_balance,
            )
            ok, add_err = player_database_result(db_manager.add(gold_row, flush=False))
            if not ok:
                return fail(add_err)
        temporary_row = row_map.get(S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE)
        if temporary_row is None:
            temporary_row = AdventurerS1Currency(
                adventurer_id=adventurer_id,
                currency_code=S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                amount=temporary_balance,
            )
            ok, add_err = player_database_result(db_manager.add(temporary_row, flush=False))
            if not ok:
                return fail(add_err)

        logs = []
        if gold_spent > 0:
            gold_row.amount = round(gold_balance - gold_spent, 2)
            S1CurrencyService._sync_gold_to_main(adventurer_id, gold_row.amount)
            logs.append(S1CurrencyService._build_log(
                adventurer_id, 'gold', -gold_spent, gold_row.amount,
                source_type, related_id, related_type, description,
            ))
        if temporary_spent > 0:
            temporary_row.amount = round(temporary_balance - temporary_spent, 2)
            logs.append(S1CurrencyService._build_log(
                adventurer_id,
                S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                -temporary_spent,
                temporary_row.amount,
                source_type,
                related_id,
                related_type,
                description,
            ))
        logs = [log for log in logs if log is not None]
        if logs:
            ok, log_err = player_database_result(db_manager.add_all(logs, flush=False))
            if not ok:
                return fail(log_err)
        if auto_commit:
            ok, commit_err = player_database_result(db_manager.commit())
            if not ok:
                return None, commit_err
        return {
            'gold_balance': round(float(gold_row.amount), 2),
            'low_consumption_balance': round(float(temporary_row.amount), 2),
            'gold_spent': gold_spent,
            'low_consumption_spent': temporary_spent,
            'total_spent': amount,
        }, None

    @staticmethod
    @db_retry()
    def _deduct_low_consumption_impl(
        adventurer_id: int,
        amount: float,
        facility_key: str,
        source_type: str,
        related_id,
        related_type,
        description,
    ) -> Tuple[Optional[dict], Optional[str]]:
        return S1CurrencyService._deduct_low_consumption_once(
            adventurer_id,
            amount,
            facility_key,
            source_type,
            related_id,
            related_type,
            description,
            True,
        )

    @staticmethod
    def deduct_low_consumption(
        adventurer_id: int,
        amount: float,
        facility_key: str,
        source_type: str,
        related_id=None,
        related_type=None,
        description=None,
        auto_commit: bool = True,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """白名单设施原子支付：非负金币优先，临时金只补足差额且绝不透支。"""
        normalized_related_id = S1CurrencyService.normalize_related_id(related_id)
        if not auto_commit:
            return S1CurrencyService._deduct_low_consumption_once(
                adventurer_id,
                amount,
                facility_key,
                source_type,
                normalized_related_id,
                related_type,
                description,
                False,
            )
        try:
            return S1CurrencyService._deduct_low_consumption_impl(
                adventurer_id,
                amount,
                facility_key,
                source_type,
                normalized_related_id,
                related_type,
                description,
            )
        except (ConcurrencyError, DatabaseError) as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def reclaim_low_consumption_split_in_transaction(
        adventurer_id: int,
        total_amount: float,
        low_consumption_credited: float,
        *,
        source_type: str,
        related_id=None,
        related_type=None,
        description: str | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """在外层事务中撤回一笔曾分流的金币收益。

        临时金是同币种合并余额，因此按确定的“当前临时金池优先”规则扣回；余额不足
        的部分由普通金币承担，普通金币可因此重新或进一步负债。总回收额始终严格等于
        原始收益。
        """
        total, err = S1CurrencyService._validate_amount(total_amount)
        if err:
            return None, err
        temporary_grant, err = S1CurrencyService._validate_amount(
            low_consumption_credited,
        )
        if err:
            return None, err
        if total <= 0:
            return None, '撤回收益必须大于0'
        if temporary_grant < 0 or temporary_grant > total + 0.001:
            return None, '临时金分流金额超出原始收益'

        locked = S1CurrencyService._lock_adventurers([adventurer_id]).get(
            int(adventurer_id),
        )
        if locked is None:
            return None, '冒险者不存在'
        rows = (
            AdventurerS1Currency.query
            .filter(
                AdventurerS1Currency.adventurer_id == adventurer_id,
                AdventurerS1Currency.currency_code.in_([
                    'gold', S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                ]),
            )
            .order_by(AdventurerS1Currency.currency_code.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        row_map = {row.currency_code: row for row in rows}
        gold_row = row_map.get('gold')
        temporary_row = row_map.get(S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE)
        if gold_row is None:
            return None, '金币余额行缺失，无法精确撤回分流收益'
        if temporary_row is None and temporary_grant > 0.001:
            return None, '临时金余额行缺失，无法精确撤回分流收益'

        raw_gold_balance = float(gold_row.amount or 0)
        raw_main_gold = float(locked.gold or 0)
        raw_temporary_balance = round(
            float(temporary_row.amount or 0) if temporary_row is not None else 0.0,
            2,
        )
        if not all(math.isfinite(value) for value in (
            raw_gold_balance,
            raw_main_gold,
            raw_temporary_balance,
        )):
            return None, '金币或临时金余额无效，无法精确撤回分流收益'
        gold_balance = round(raw_gold_balance, 2)
        main_gold = round(raw_main_gold, 2)
        if gold_balance != main_gold:
            return None, '主表金币与货币账本不一致，无法精确撤回分流收益'
        if (
            raw_temporary_balance < -0.001
            or raw_temporary_balance
            > S1CurrencyService.low_consumption_cap() + 0.001
        ):
            return None, '临时金余额超出合法范围，无法精确撤回分流收益'
        temporary_balance = round(max(0.0, raw_temporary_balance), 2)
        temporary_reclaimed = round(min(temporary_grant, temporary_balance), 2)
        gold_reclaimed = round(total - temporary_reclaimed, 2)
        logs = []
        if temporary_reclaimed > 0:
            if temporary_row is None:
                return None, '临时金余额行缺失，无法精确撤回分流收益'
            temporary_row.amount = round(temporary_balance - temporary_reclaimed, 2)
            logs.append(S1CurrencyService._build_log(
                adventurer_id,
                S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                -temporary_reclaimed,
                temporary_row.amount,
                source_type,
                related_id,
                related_type,
                description,
            ))
        if gold_reclaimed > 0:
            gold_row.amount = round(gold_balance - gold_reclaimed, 2)
            S1CurrencyService._sync_gold_to_main(adventurer_id, gold_row.amount)
            logs.append(S1CurrencyService._build_log(
                adventurer_id,
                'gold',
                -gold_reclaimed,
                gold_row.amount,
                source_type,
                related_id,
                related_type,
                description,
            ))
        ok, log_err = player_database_result(db_manager.add_all(
            [log for log in logs if log is not None],
            flush=False,
        ))
        if not ok:
            return None, log_err
        return {
            'gold_balance': round(float(gold_row.amount or 0), 2),
            'low_consumption_balance': round(
                float(temporary_row.amount or 0)
                if temporary_row is not None
                else 0.0,
                2,
            ),
            'gold_reclaimed': gold_reclaimed,
            'low_consumption_reclaimed': temporary_reclaimed,
            'total_reclaimed': total,
        }, None

    # ── 公开接口 ────────────────────────────────────────────────────────────

    @staticmethod
    def get_balance(adventurer_id: int, currency_code: str) -> Tuple[Optional[float], Optional[str]]:
        """查询冒险者某货币余额。返回 (balance, error)。

        gold 在 Currency 记录缺失时回退读主表（双轨兜底），其他货币缺失视为 0。
        """
        try:
            rec = AdventurerS1Currency.query.filter_by(
                adventurer_id=adventurer_id, currency_code=currency_code,
            ).first()
            if rec is not None:
                return round(float(rec.amount), 2), None
            return S1CurrencyService._initial_amount_for(adventurer_id, currency_code), None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def get_all_balances(adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """查询冒险者所有货币余额。返回 ({'tavern_coin': 50.0, ...}, error)。"""
        try:
            records = AdventurerS1Currency.query.filter_by(adventurer_id=adventurer_id).all()
            result = {r.currency_code: round(float(r.amount), 2) for r in records}
            # 补全配置中存在但数据库中还没有的货币
            currency_types = current_app.config.get('ADVENTURER_S1_CURRENCY_TYPES', {})
            for code, info in currency_types.items():
                if code not in result:
                    result[code] = round(float(info.get('initial', 0)), 2)
            return result, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    # 自管事务调用 _once 版本，禁止局部 rollback/retry；独立货币事务才使用 @db_retry。

    @staticmethod
    def _add_once(adventurer_id: int, currency_code: str, amount: float,
                  source_type: str, related_id, related_type, description,
                  auto_commit: bool, silent: bool = False,
                  split_result: Optional[dict] = None) -> Tuple[Optional[float], Optional[str]]:
        if split_result is not None:
            split_result.clear()
        reserved_err = S1CurrencyService._reserved_low_consumption_error(currency_code)
        if reserved_err:
            return None, reserved_err
        validated, err = S1CurrencyService._validate_amount(amount)
        if err:
            return None, err
        amount = validated
        if amount < 0:
            return None, '增加金额必须大于0'
        original_amount = amount

        S1CurrencyService._lock_adventurers([adventurer_id])
        rec = AdventurerS1Currency.query.filter_by(
            adventurer_id=adventurer_id, currency_code=currency_code,
        ).populate_existing().with_for_update().first()

        if rec is None:
            rec = AdventurerS1Currency(
                adventurer_id=adventurer_id,
                currency_code=currency_code,
                amount=S1CurrencyService._initial_amount_for(adventurer_id, currency_code),
            )
            ok, err = player_database_result(db_manager.add(rec))
            if not ok:
                return None, err

        split_plan = {'row_map': {}, 'final_balances': {}, 'credits': {}}
        if currency_code == 'gold':
            adjusted, split_plan = S1CurrencyService._plan_low_consumption_split(
                {int(adventurer_id): [{
                    'amount': amount,
                    'source_type': source_type,
                    'related_id': related_id,
                    'related_type': related_type,
                    'silent': silent,
                }]},
                {int(adventurer_id): round(float(rec.amount), 2)},
                amount_key='amount',
            )
            amount = adjusted[int(adventurer_id)][0]['amount']

        if split_result is not None:
            temporary_credited = round(sum(
                float(entry.get('amount') or 0)
                for entry in (split_plan.get('credits') or {}).get(
                    int(adventurer_id),
                    [],
                )
            ), 2)
            split_result.update({
                'requested_amount': original_amount,
                'gold_credited': amount if currency_code == 'gold' else 0.0,
                'low_consumption_credited': temporary_credited,
            })

        rec.amount = round(float(rec.amount) + amount, 2)
        if currency_code == 'gold':
            S1CurrencyService._sync_gold_to_main(adventurer_id, rec.amount)

        S1CurrencyService._log(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
            change_amount=amount,
            balance_after=rec.amount,
            source_type=source_type,
            related_id=related_id,
            related_type=related_type,
            description=description,
            silent=silent,
        )

        if currency_code == 'gold':
            S1CurrencyService._apply_low_consumption_split(split_plan)

        if auto_commit:
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
        return round(float(rec.amount), 2), None

    @staticmethod
    @db_retry()
    def _add_impl(adventurer_id: int, currency_code: str, amount: float,
                  source_type: str, related_id, related_type, description,
                  auto_commit: bool, silent: bool = False,
                  split_result: Optional[dict] = None) -> Tuple[Optional[float], Optional[str]]:
        return S1CurrencyService._add_once(
            adventurer_id,
            currency_code,
            amount,
            source_type,
            related_id,
            related_type,
            description,
            auto_commit,
            silent,
            split_result,
        )

    @staticmethod
    def add(adventurer_id: int, currency_code: str, amount: float,
            source_type: str, related_id=None, related_type=None, description=None,
            auto_commit: bool = True, silent: bool = False,
            split_result: Optional[dict] = None) -> Tuple[Optional[float], Optional[str]]:
        """增加货币。返回 (new_balance, error)。"""
        normalized_related_id = S1CurrencyService.normalize_related_id(related_id)
        if not auto_commit:
            return S1CurrencyService._add_once(
                adventurer_id, currency_code, amount, source_type,
                normalized_related_id, related_type, description, False, silent,
                split_result,
            )
        try:
            return S1CurrencyService._add_impl(
                adventurer_id, currency_code, amount, source_type,
                normalized_related_id,
                related_type, description, auto_commit, silent, split_result,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            # flush/autoflush 产生的 DBAPI/SQLAlchemy 异常不一定已经包装成 DatabaseError；
            # 若不回滚，调用方后续任何 ORM 访问都会变成 PendingRollbackError。
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def _add_many_once(adventurer_id: int, currency_code: str, entries: list[dict],
                       auto_commit: bool) -> Tuple[Optional[float], Optional[str]]:
        reserved_err = S1CurrencyService._reserved_low_consumption_error(currency_code)
        if reserved_err:
            return None, reserved_err
        if not isinstance(entries, list) or not entries:
            return None, '批量入账明细不能为空'

        normalized = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return None, f'批量入账明细[{index}]必须是对象'
            amount, err = S1CurrencyService._validate_amount(entry.get('amount'))
            if err:
                return None, f'批量入账明细[{index}]：{err}'
            if amount < 0:
                return None, f'批量入账明细[{index}]：增加金额必须大于0'
            source_type = str(entry.get('source_type') or '').strip()
            if not source_type:
                return None, f'批量入账明细[{index}]缺少 source_type'
            business_payload = entry.get('business_payload')
            if business_payload is not None and not isinstance(business_payload, dict):
                return None, f'批量入账明细[{index}]的 business_payload 必须是对象'
            business_expires_at = entry.get('business_expires_at')
            if business_expires_at is not None and not isinstance(
                business_expires_at, datetime,
            ):
                return None, f'批量入账明细[{index}]的 business_expires_at 必须是时间'
            normalized.append({
                'amount': amount,
                'source_type': source_type,
                'related_id': S1CurrencyService.normalize_related_id(entry.get('related_id')),
                'related_type': entry.get('related_type'),
                'description': entry.get('description'),
                'silent': bool(entry.get('silent', False)),
                'business_payload': (
                    dict(business_payload) if isinstance(business_payload, dict) else None
                ),
                'business_expires_at': business_expires_at,
            })

        S1CurrencyService._lock_adventurers([adventurer_id])
        rec = AdventurerS1Currency.query.filter_by(
            adventurer_id=adventurer_id,
            currency_code=currency_code,
        ).populate_existing().with_for_update().first()
        if rec is None:
            rec = AdventurerS1Currency(
                adventurer_id=adventurer_id,
                currency_code=currency_code,
                amount=S1CurrencyService._initial_amount_for(adventurer_id, currency_code),
            )
            ok, err = player_database_result(db_manager.add(rec))
            if not ok:
                return None, err

        split_plan = {'row_map': {}, 'final_balances': {}, 'credits': {}}
        if currency_code == 'gold':
            adjusted, split_plan = S1CurrencyService._plan_low_consumption_split(
                {int(adventurer_id): normalized},
                {int(adventurer_id): round(float(rec.amount), 2)},
                amount_key='amount',
            )
            normalized = adjusted[int(adventurer_id)]

        running_balance = round(float(rec.amount), 2)
        logs = []
        for entry in normalized:
            running_balance = round(running_balance + entry['amount'], 2)
            log = S1CurrencyService._build_log(
                adventurer_id=adventurer_id,
                currency_code=currency_code,
                change_amount=entry['amount'],
                balance_after=running_balance,
                source_type=entry['source_type'],
                related_id=entry['related_id'],
                related_type=entry['related_type'],
                description=entry['description'],
                silent=entry['silent'],
                business_payload=entry['business_payload'],
                business_expires_at=entry['business_expires_at'],
            )
            if log is not None:
                logs.append(log)

        rec.amount = running_balance
        if currency_code == 'gold':
            S1CurrencyService._sync_gold_to_main(adventurer_id, rec.amount)
            S1CurrencyService._apply_low_consumption_split(split_plan)
        if logs:
            ok, err = player_database_result(db_manager.add_all(logs))
            if not ok:
                return None, err
        if auto_commit:
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
        return round(float(rec.amount), 2), None

    @staticmethod
    @db_retry()
    def _add_many_impl(adventurer_id: int, currency_code: str, entries: list[dict],
                       auto_commit: bool) -> Tuple[Optional[float], Optional[str]]:
        return S1CurrencyService._add_many_once(
            adventurer_id,
            currency_code,
            entries,
            auto_commit,
        )

    @staticmethod
    def add_many_in_transaction(
        adventurer_id: int,
        currency_code: str,
        entries: list[dict],
    ) -> Tuple[Optional[float], Optional[str]]:
        """调用方自管事务的批量入账；不提交、回滚或局部重试。"""
        return S1CurrencyService._add_many_once(
            adventurer_id,
            currency_code,
            entries,
            False,
        )

    @staticmethod
    def apply_changes_in_transaction(
        currency_code: str,
        entries: list[dict],
    ) -> Tuple[Optional[dict[int, float]], Optional[str]]:
        """在调用方事务内按冒险者 ID 升序锁定余额并应用跨冒险者变更。

        调用方必须先按同一升序锁定所有相关 AdventurerS1 行。本方法不提交、回滚或
        局部重试。明细可显式传 ``allow_negative=True`` 允许该笔扣成负数；默认余额
        不足时不修改任何余额。
        """
        reserved_err = S1CurrencyService._reserved_low_consumption_error(currency_code)
        if reserved_err:
            return None, reserved_err
        if not isinstance(entries, list) or not entries:
            return None, '货币变更明细不能为空'

        grouped: dict[int, list[dict]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return None, f'货币变更明细[{index}]必须是对象'
            adventurer_id = entry.get('adventurer_id')
            if (
                isinstance(adventurer_id, bool)
                or not isinstance(adventurer_id, int)
                or adventurer_id <= 0
            ):
                return None, f'货币变更明细[{index}].adventurer_id 必须是正整数'
            change_amount, err = S1CurrencyService._validate_amount(entry.get('change_amount'))
            if err:
                return None, f'货币变更明细[{index}]：{err}'
            if change_amount == 0:
                return None, f'货币变更明细[{index}]：变更金额不能为 0'
            source_type = str(entry.get('source_type') or '').strip()
            if not source_type:
                return None, f'货币变更明细[{index}]缺少 source_type'
            item = {
                'adventurer_id': adventurer_id,
                'change_amount': change_amount,
                'source_type': source_type,
                'related_id': S1CurrencyService.normalize_related_id(entry.get('related_id')),
                'related_type': entry.get('related_type'),
                'description': entry.get('description'),
                'silent': bool(entry.get('silent', False)),
                'insufficient_error': entry.get('insufficient_error'),
                'allow_negative': bool(entry.get('allow_negative', False)),
            }
            grouped.setdefault(adventurer_id, []).append(item)

        adventurer_ids = sorted(grouped)
        rows = (
            AdventurerS1Currency.query
            .filter(
                AdventurerS1Currency.adventurer_id.in_(adventurer_ids),
                AdventurerS1Currency.currency_code == currency_code,
            )
            .order_by(AdventurerS1Currency.adventurer_id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        row_map = {int(row.adventurer_id): row for row in rows}

        split_plan = {'row_map': {}, 'final_balances': {}, 'credits': {}}
        if currency_code == 'gold':
            starting_balances = {
                adventurer_id: round(
                    float(row_map[adventurer_id].amount)
                    if adventurer_id in row_map
                    else S1CurrencyService._initial_amount_for(adventurer_id, currency_code),
                    2,
                )
                for adventurer_id in adventurer_ids
            }
            grouped, split_plan = S1CurrencyService._plan_low_consumption_split(
                grouped,
                starting_balances,
                amount_key='change_amount',
            )

        projected_balances = {}
        for adventurer_id in adventurer_ids:
            row = row_map.get(adventurer_id)
            running_balance = round(
                float(row.amount) if row is not None
                else S1CurrencyService._initial_amount_for(adventurer_id, currency_code),
                2,
            )
            for entry in grouped[adventurer_id]:
                running_balance = round(running_balance + entry['change_amount'], 2)
                # 负余额是合法债务；正向入账可逐步偿还，只有扣款不得继续透支。
                if (
                    entry['change_amount'] < 0
                    and running_balance < -0.001
                    and not entry['allow_negative']
                ):
                    return None, (
                        str(entry['insufficient_error'])
                        if entry.get('insufficient_error')
                        else f'{currency_code} 余额不足'
                    )
            projected_balances[adventurer_id] = running_balance

        logs = []
        final_balances = {}
        for adventurer_id in adventurer_ids:
            row = row_map.get(adventurer_id)
            if row is None:
                row = AdventurerS1Currency(
                    adventurer_id=adventurer_id,
                    currency_code=currency_code,
                    amount=S1CurrencyService._initial_amount_for(adventurer_id, currency_code),
                )
                ok, err = player_database_result(db_manager.add(row, flush=False))
                if not ok:
                    return None, err
            running_balance = round(float(row.amount), 2)
            for entry in grouped[adventurer_id]:
                running_balance = round(running_balance + entry['change_amount'], 2)
                log = S1CurrencyService._build_log(
                    adventurer_id=adventurer_id,
                    currency_code=currency_code,
                    change_amount=entry['change_amount'],
                    balance_after=running_balance,
                    source_type=entry['source_type'],
                    related_id=entry['related_id'],
                    related_type=entry['related_type'],
                    description=entry['description'],
                    silent=entry['silent'],
                )
                if log is not None:
                    logs.append(log)
            row.amount = projected_balances[adventurer_id]
            if currency_code == 'gold':
                S1CurrencyService._sync_gold_to_main(adventurer_id, row.amount)
            final_balances[adventurer_id] = round(float(row.amount), 2)

        if currency_code == 'gold':
            S1CurrencyService._apply_low_consumption_split(split_plan)

        if logs:
            ok, err = player_database_result(db_manager.add_all(logs, flush=False))
            if not ok:
                return None, err
        return final_balances, None

    @staticmethod
    def add_many(adventurer_id: int, currency_code: str, entries: list[dict],
                 auto_commit: bool = True) -> Tuple[Optional[float], Optional[str]]:
        """一次锁定余额并批量增加多笔金额，同时保留每笔独立流水。"""
        if not auto_commit:
            return S1CurrencyService._add_many_once(
                adventurer_id,
                currency_code,
                entries,
                False,
            )
        try:
            return S1CurrencyService._add_many_impl(
                adventurer_id,
                currency_code,
                entries,
                auto_commit,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    @db_retry()
    def _deduct_impl(adventurer_id: int, currency_code: str, amount: float,
                     source_type: str, related_id, related_type, description,
                     auto_commit: bool, allow_negative: bool = False,
                     silent: bool = False,
                     ) -> Tuple[Optional[float], Optional[str]]:
        balance, err, _log = S1CurrencyService._deduct_once(
            adventurer_id,
            currency_code,
            amount,
            source_type,
            related_id,
            related_type,
            description,
            auto_commit,
            allow_negative,
            silent,
            False,
        )
        return balance, err

    @staticmethod
    def deduct_in_transaction(
        adventurer_id: int,
        currency_code: str,
        amount: float,
        source_type: str,
        related_id=None,
        related_type=None,
        description=None,
        allow_negative: bool = False,
        silent: bool = False,
    ):
        """调用方自管事务的单次扣款。

        不 commit、不 rollback、不做局部重试，并返回本次 CurrencyLog 对象，供调用方在
        同一事务稍后补齐刚创建的业务实体 ID。flush 异常不转换为错误字符串，必须由
        外层把完整业务事务一起回滚或重试。
        """
        return S1CurrencyService._deduct_once(
            adventurer_id,
            currency_code,
            amount,
            source_type,
            S1CurrencyService.normalize_related_id(related_id),
            related_type,
            description,
            False,
            allow_negative,
            silent,
            True,
        )

    @staticmethod
    def deduct(adventurer_id: int, currency_code: str, amount: float,
               source_type: str, related_id=None, related_type=None, description=None,
               auto_commit: bool = True, allow_negative: bool = False,
               silent: bool = False,
               ) -> Tuple[Optional[float], Optional[str]]:
        """扣除货币；allow_negative=True 时允许透支。返回 (new_balance, error)。"""
        normalized_related_id = S1CurrencyService.normalize_related_id(related_id)
        if not auto_commit:
            balance, err, _log = S1CurrencyService._deduct_once(
                adventurer_id,
                currency_code,
                amount,
                source_type,
                normalized_related_id,
                related_type,
                description,
                False,
                allow_negative,
                silent,
                False,
            )
            return balance, err
        try:
            return S1CurrencyService._deduct_impl(
                adventurer_id, currency_code, amount, source_type,
                normalized_related_id,
                related_type, description, auto_commit, allow_negative,
                silent=silent,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    @db_retry()
    def _transfer_impl(from_adv_id: int, to_adv_id: int, currency_code: str, amount: float,
                       source_type: str, description) -> Tuple[Optional[dict], Optional[str]]:
        reserved_err = S1CurrencyService._reserved_low_consumption_error(currency_code)
        if reserved_err:
            return None, reserved_err
        validated, err = S1CurrencyService._validate_amount(amount)
        if err:
            return None, err
        amount = validated
        if amount <= 0:
            return None, '转账金额必须大于0'

        # 固定顺序加锁，避免死锁
        ids = sorted([from_adv_id, to_adv_id])
        S1CurrencyService._lock_adventurers(ids)
        recs = {}
        for aid in ids:
            rec = AdventurerS1Currency.query.filter_by(
                adventurer_id=aid, currency_code=currency_code,
            ).populate_existing().with_for_update().first()
            if rec is None:
                rec = AdventurerS1Currency(
                    adventurer_id=aid, currency_code=currency_code,
                    amount=S1CurrencyService._initial_amount_for(aid, currency_code),
                )
                ok, add_err = player_database_result(db_manager.add(rec))
                if not ok:
                    db_manager.rollback()
                    return None, add_err
            recs[aid] = rec

        from_rec = recs[from_adv_id]
        if float(from_rec.amount) < amount - 0.001:
            db_manager.rollback()
            return None, f'{currency_code} 余额不足'

        from_rec.amount = round(float(from_rec.amount) - amount, 2)
        to_rec = recs[to_adv_id]
        to_rec.amount = round(float(to_rec.amount) + amount, 2)
        if currency_code == 'gold':
            S1CurrencyService._sync_gold_to_main(from_adv_id, from_rec.amount)
            S1CurrencyService._sync_gold_to_main(to_adv_id, to_rec.amount)

        desc_out = description or f'转账给冒险者#{to_adv_id}'
        desc_in = description or f'来自冒险者#{from_adv_id}'
        S1CurrencyService._log(from_adv_id, currency_code, -amount, from_rec.amount,
                               f'{source_type}_out', related_id=to_adv_id, description=desc_out)
        S1CurrencyService._log(to_adv_id, currency_code, amount, to_rec.amount,
                               f'{source_type}_in', related_id=from_adv_id, description=desc_in)

        ok, err = player_database_result(db_manager.commit())
        if not ok:
            return None, err
        return {
            'from_balance': round(float(from_rec.amount), 2),
            'to_balance': round(float(to_rec.amount), 2),
        }, None

    @staticmethod
    def transfer(from_adv_id: int, to_adv_id: int, currency_code: str, amount: float,
                 source_type: str = 'transfer', description: str = None) -> Tuple[Optional[dict], Optional[str]]:
        """冒险者之间转账。原子操作。返回 ({from_balance, to_balance}, error)。"""
        try:
            return S1CurrencyService._transfer_impl(
                from_adv_id, to_adv_id, currency_code, amount, source_type, description,
            )
        except ConcurrencyError as e:
            db_manager.rollback()
            return None, player_operation_failure(e)
        except DatabaseError as e:
            db_manager.rollback()
            return None, player_operation_failure(e)
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e)

    @staticmethod
    def init_currency(adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """为新冒险者初始化所有货币。

        gold 使用主表 `AdventurerS1.gold` 作为权威初始值（双轨一致），其他货币
        从 CURRENCY_TYPES 配置的 initial 字段读取。
        """
        try:
            with db_manager.transaction():
                currency_types = current_app.config.get('ADVENTURER_S1_CURRENCY_TYPES', {})
                locked_adv = S1CurrencyService._lock_adventurers([adventurer_id]).get(
                    int(adventurer_id),
                )
                if locked_adv is None:
                    raise S1PlayerVisibleError('冒险者不存在')

                currency_codes = list(currency_types)
                existing_rows = (
                    AdventurerS1Currency.query
                    .filter(
                        AdventurerS1Currency.adventurer_id == adventurer_id,
                        AdventurerS1Currency.currency_code.in_(currency_codes),
                    )
                    .order_by(AdventurerS1Currency.currency_code.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                    if currency_codes
                    else []
                )
                existing_by_code = {row.currency_code: row for row in existing_rows}
                initial_gold = round(float(locked_adv.gold or 0.0), 2)
                result = {}
                records = []
                for code in currency_codes:
                    existing = existing_by_code.get(code)
                    if existing is not None:
                        result[code] = round(float(existing.amount), 2)
                        continue
                    initial = initial_gold if code == 'gold' else 0.0
                    records.append(AdventurerS1Currency(
                        adventurer_id=adventurer_id,
                        currency_code=code,
                        amount=initial,
                    ))
                    if initial > 0:
                        records.append(AdventurerS1CurrencyLog(
                            adventurer_id=adventurer_id,
                            currency_code=code,
                            change_amount=initial,
                            balance_after=initial,
                            source_type='init',
                            description='冒险者创建初始化',
                        ))
                    result[code] = initial
                if records:
                    ok, err = db_manager.add_all(records)
                    if not ok:
                        raise RuntimeError(err)
            return result, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def get_logs(adventurer_id: int, currency_code: str = None,
                 source_type: str = None, limit: int = 50) -> Tuple[Optional[list], Optional[str]]:
        """查询货币变动日志。返回 ([log_dict, ...], error)。"""
        try:
            max_limit = max(
                1,
                int(current_app.config.get('ADVENTURER_S1_CURRENCY_LOG_LIMIT_MAX', 200)),
            )
            limit = max(1, min(int(limit or 50), max_limit))
            query = AdventurerS1CurrencyLog.query.filter_by(adventurer_id=adventurer_id)
            if currency_code:
                query = query.filter_by(currency_code=currency_code)
            if source_type:
                query = query.filter_by(source_type=source_type)
            logs = query.order_by(AdventurerS1CurrencyLog.created_at.desc()).limit(limit).all()
            result = [log.to_dict() for log in logs]
            from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                sanitize_player_currency_log_entries,
            )

            sanitize_player_currency_log_entries(result)
            return result, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def get_currency_types() -> Tuple[Optional[list], Optional[str]]:
        """获取所有货币类型定义。"""
        try:
            currency_types = current_app.config.get('ADVENTURER_S1_CURRENCY_TYPES', {})
            result = [
                {'code': code, 'name': info.get('name', code), 'icon': info.get('icon', '')}
                for code, info in currency_types.items()
            ]
            return result, None
        except Exception as e:
            return None, player_operation_failure(e, '操作失败')
