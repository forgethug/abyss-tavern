"""深渊赌坊金币账本预览与幂等校正。"""

from __future__ import annotations

from sqlalchemy import Float, String, and_, case, cast, func

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1CurrencyLog,
    AdventurerS1GamblingBet,
    AdventurerS1GamblingRound,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)


class _PreviewChanged(RuntimeError):
    """预览后账本发生变化，整批停止。"""


class S1GamblingGoldReconciliationService:
    """复用 ``repair_s1_gambling_overpay.py`` 的逐轮净额口径。"""

    OPERATION = 'gambling_gold_reconciliation'
    LEDGER_SOURCE_TYPES = (
        'gambling_bet',
        'gambling_win',
        'gambling_refund',
        'gambling_clawback',
    )
    SOURCE_TYPE = 'gambling_clawback'
    PREVIEW_ITEM_LIMIT = 100

    @staticmethod
    def _normalize_adventurer_id(value):
        if value in (None, ''):
            return None, None
        if isinstance(value, bool):
            return None, '冒险者 ID 必须是正整数'
        try:
            adventurer_id = int(value)
        except (TypeError, ValueError):
            return None, '冒险者 ID 必须是正整数'
        if adventurer_id <= 0:
            return None, '冒险者 ID 必须是正整数'
        return adventurer_id, None

    @classmethod
    def _candidate_rows(cls, *, adventurer_id=None, round_ids=None):
        normalized_round_ids = None
        if round_ids is not None:
            normalized_round_ids = sorted({int(value) for value in round_ids})
            if not normalized_round_ids:
                return []

        ledger_query = (
            db_manager.query(
                AdventurerS1CurrencyLog.related_id.label('related_id'),
                AdventurerS1CurrencyLog.adventurer_id.label('adventurer_id'),
                func.sum(AdventurerS1CurrencyLog.change_amount).label('actual_net'),
                func.sum(case(
                    (AdventurerS1CurrencyLog.source_type == 'gambling_bet', 1),
                    else_=0,
                )).label('n_bet'),
                func.sum(case(
                    (
                        AdventurerS1CurrencyLog.source_type == 'gambling_bet',
                        AdventurerS1CurrencyLog.change_amount,
                    ),
                    else_=0.0,
                )).label('bet_net'),
                func.sum(case(
                    (AdventurerS1CurrencyLog.source_type == 'gambling_win', 1),
                    else_=0,
                )).label('n_win'),
                func.sum(case(
                    (AdventurerS1CurrencyLog.source_type == 'gambling_refund', 1),
                    else_=0,
                )).label('n_refund'),
                func.sum(case(
                    (AdventurerS1CurrencyLog.source_type == 'gambling_clawback', 1),
                    else_=0,
                )).label('n_clawback'),
            )
            .filter(
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.related_type == 'gambling_round',
                AdventurerS1CurrencyLog.source_type.in_(cls.LEDGER_SOURCE_TYPES),
            )
        )
        if adventurer_id is not None:
            ledger_query = ledger_query.filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
            )
        if normalized_round_ids is not None:
            ledger_query = ledger_query.filter(
                AdventurerS1CurrencyLog.related_id.in_(
                    [str(value) for value in normalized_round_ids],
                ),
            )
        ledger = (
            ledger_query
            .group_by(
                AdventurerS1CurrencyLog.related_id,
                AdventurerS1CurrencyLog.adventurer_id,
            )
            .subquery()
        )

        expected_net = case(
            (AdventurerS1GamblingRound.status == 'cancelled', 0.0),
            (
                and_(
                    AdventurerS1GamblingRound.outcome.in_(('high', 'low')),
                    AdventurerS1GamblingRound.outcome == AdventurerS1GamblingBet.choice,
                ),
                cast(AdventurerS1GamblingBet.bet_amount, Float),
            ),
            else_=-cast(AdventurerS1GamblingBet.bet_amount, Float),
        )
        actual_net = func.coalesce(ledger.c.actual_net, 0.0)
        overpay = actual_net - expected_net

        query = (
            db_manager.query(
                AdventurerS1GamblingRound.id.label('round_id'),
                AdventurerS1GamblingRound.round_epoch.label('round_epoch'),
                AdventurerS1GamblingRound.status.label('round_status'),
                AdventurerS1GamblingRound.outcome.label('round_outcome'),
                AdventurerS1GamblingBet.adventurer_id.label('adventurer_id'),
                AdventurerS1.name.label('adventurer_name'),
                AdventurerS1GamblingBet.choice.label('choice'),
                AdventurerS1GamblingBet.bet_amount.label('bet_amount'),
                expected_net.label('expected_net'),
                actual_net.label('actual_net'),
                func.coalesce(ledger.c.n_bet, 0).label('n_bet'),
                func.coalesce(ledger.c.bet_net, 0.0).label('bet_net'),
                func.coalesce(ledger.c.n_win, 0).label('n_win'),
                func.coalesce(ledger.c.n_refund, 0).label('n_refund'),
                func.coalesce(ledger.c.n_clawback, 0).label('n_clawback'),
                overpay.label('overpay'),
            )
            .select_from(AdventurerS1GamblingBet)
            .join(
                AdventurerS1GamblingRound,
                AdventurerS1GamblingRound.id == AdventurerS1GamblingBet.round_id,
            )
            .join(
                AdventurerS1,
                AdventurerS1.id == AdventurerS1GamblingBet.adventurer_id,
            )
            .outerjoin(
                ledger,
                and_(
                    ledger.c.related_id == cast(AdventurerS1GamblingRound.id, String),
                    ledger.c.adventurer_id == AdventurerS1GamblingBet.adventurer_id,
                ),
            )
            .filter(
                AdventurerS1GamblingRound.status.in_(('settled', 'cancelled')),
                overpay > 0.001,
            )
        )
        if adventurer_id is not None:
            query = query.filter(AdventurerS1GamblingBet.adventurer_id == adventurer_id)
        if normalized_round_ids is not None:
            query = query.filter(AdventurerS1GamblingRound.id.in_(normalized_round_ids))

        rows = query.order_by(
            AdventurerS1GamblingBet.adventurer_id.asc(),
            AdventurerS1GamblingRound.id.asc(),
        ).all()
        return [
            {
                'round_id': int(row.round_id),
                'round_epoch': int(row.round_epoch),
                'round_status': str(row.round_status),
                'round_outcome': str(row.round_outcome or ''),
                'adventurer_id': int(row.adventurer_id),
                'adventurer_name': str(row.adventurer_name or ''),
                'choice': str(row.choice),
                'bet_amount': int(row.bet_amount),
                'expected_net': round(float(row.expected_net or 0), 2),
                'actual_net': round(float(row.actual_net or 0), 2),
                'overpay': round(float(row.overpay or 0), 2),
                'n_bet': int(row.n_bet or 0),
                'bet_net': round(float(row.bet_net or 0), 2),
                'n_win': int(row.n_win or 0),
                'n_refund': int(row.n_refund or 0),
                'n_clawback': int(row.n_clawback or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _canonical_items(items):
        return [
            {
                'round_id': int(item['round_id']),
                'round_epoch': int(item['round_epoch']),
                'round_status': str(item['round_status']),
                'round_outcome': str(item['round_outcome']),
                'adventurer_id': int(item['adventurer_id']),
                'choice': str(item['choice']),
                'bet_amount': int(item['bet_amount']),
                'expected_net': round(float(item['expected_net']), 2),
                'actual_net': round(float(item['actual_net']), 2),
                'overpay': round(float(item['overpay']), 2),
                'n_bet': int(item['n_bet']),
                'bet_net': round(float(item['bet_net']), 2),
                'n_win': int(item['n_win']),
                'n_refund': int(item['n_refund']),
                'n_clawback': int(item['n_clawback']),
            }
            for item in items
        ]

    @staticmethod
    def _is_safe_item(item):
        round_status = str(item['round_status'])
        round_outcome = str(item['round_outcome'])
        valid_final_state = (
            round_status == 'settled'
            and round_outcome in ('high', 'low', 'house')
        ) or (
            round_status == 'cancelled'
            and round_outcome == 'cancelled'
        )
        return (
            valid_final_state
            and str(item['choice']) in ('high', 'low')
            and int(item['bet_amount']) > 0
            and int(item['n_bet']) == 1
            and abs(float(item['bet_net']) + float(item['bet_amount'])) <= 0.001
        )

    @classmethod
    def preview(cls, admin_user_id: int | None = None, adventurer_id=None):
        normalized_id, err = cls._normalize_adventurer_id(adventurer_id)
        if err:
            return None, err
        items = cls._candidate_rows(adventurer_id=normalized_id)
        unsafe_items = [item for item in items if not cls._is_safe_item(item)]
        display_items = sorted(
            items,
            key=lambda item: (
                cls._is_safe_item(item),
                item['adventurer_id'],
                item['round_id'],
            ),
        )
        result = {
            'scope_adventurer_id': normalized_id,
            'candidate_count': len(items),
            'adventurer_count': len({item['adventurer_id'] for item in items}),
            'total_overpay': round(sum(item['overpay'] for item in items), 2),
            'unsafe_candidate_count': len(unsafe_items),
            'missing_bet_rows': sum(item['n_bet'] == 0 for item in items),
            'duplicate_bet_rows': sum(item['n_bet'] > 1 for item in items),
            'bet_amount_mismatch_rows': sum(
                item['n_bet'] == 1
                and abs(item['bet_net'] + item['bet_amount']) > 0.001
                for item in items
            ),
            'invalid_round_rows': sum(
                not (
                    (
                        item['round_status'] == 'settled'
                        and item['round_outcome'] in ('high', 'low', 'house')
                    ) or (
                        item['round_status'] == 'cancelled'
                        and item['round_outcome'] == 'cancelled'
                    )
                )
                or item['choice'] not in ('high', 'low')
                or item['bet_amount'] <= 0
                for item in items
            ),
            'safe_to_execute': not unsafe_items,
            'items': [
                {**item, 'safe_to_execute': cls._is_safe_item(item)}
                for item in display_items[:cls.PREVIEW_ITEM_LIMIT]
            ],
            'items_truncated': len(items) > cls.PREVIEW_ITEM_LIMIT,
        }
        if admin_user_id is not None and items and not unsafe_items:
            result['snapshot_token'] = S1AdminScanSnapshotService.store(
                cls.OPERATION,
                admin_user_id,
                {
                    'scope_adventurer_id': normalized_id,
                    'items': cls._canonical_items(items),
                },
            )
        else:
            result['snapshot_token'] = None
        return result, None

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        snapshot = S1AdminScanSnapshotService.load(
            cls.OPERATION,
            admin_user_id,
            preview_token,
        )
        if snapshot is None:
            return None, '预览快照已过期，请重新预览后再执行'
        snapshot_items = snapshot.get('items')
        if not isinstance(snapshot_items, list) or not snapshot_items:
            return None, '预览快照没有可执行的校正明细，请重新预览'
        try:
            canonical_snapshot = cls._canonical_items(snapshot_items)
            round_ids = sorted({item['round_id'] for item in canonical_snapshot})
            adventurer_ids = sorted({item['adventurer_id'] for item in canonical_snapshot})
            scope_id, scope_err = cls._normalize_adventurer_id(
                snapshot.get('scope_adventurer_id'),
            )
            if scope_err:
                return None, '预览快照范围无效，请重新预览'
        except (KeyError, TypeError, ValueError):
            return None, '预览快照格式无效，请重新预览'

        try:
            with db_manager.transaction():
                locked_rounds = (
                    db_manager.query(AdventurerS1GamblingRound)
                    .filter(AdventurerS1GamblingRound.id.in_(round_ids))
                    .order_by(AdventurerS1GamblingRound.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                if [int(row.id) for row in locked_rounds] != round_ids:
                    raise _PreviewChanged()
                (
                    db_manager.query(AdventurerS1GamblingBet)
                    .filter(AdventurerS1GamblingBet.round_id.in_(round_ids))
                    .order_by(
                        AdventurerS1GamblingBet.round_id.asc(),
                        AdventurerS1GamblingBet.adventurer_id.asc(),
                    )
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                locked_adventurers = (
                    db_manager.query(AdventurerS1)
                    .filter(AdventurerS1.id.in_(adventurer_ids))
                    .order_by(AdventurerS1.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                if [int(row.id) for row in locked_adventurers] != adventurer_ids:
                    raise _PreviewChanged()

                current_items = cls._candidate_rows(
                    adventurer_id=scope_id,
                    round_ids=round_ids,
                )
                if cls._canonical_items(current_items) != canonical_snapshot:
                    raise _PreviewChanged()
                if any(not cls._is_safe_item(item) for item in current_items):
                    raise _PreviewChanged()

                balances, currency_err = S1CurrencyService.apply_changes_in_transaction(
                    'gold',
                    [
                        {
                            'adventurer_id': item['adventurer_id'],
                            'change_amount': -item['overpay'],
                            'source_type': cls.SOURCE_TYPE,
                            'related_id': item['round_id'],
                            'related_type': 'gambling_round',
                            'description': (
                                f'深渊赌坊第 {item["round_epoch"]} 轮超额派彩追缴 '
                                f'{item["overpay"]:.2f} 金'
                            ),
                            'allow_negative': True,
                        }
                        for item in current_items
                    ],
                )
                if currency_err:
                    raise RuntimeError(currency_err)
        except _PreviewChanged:
            S1AdminScanSnapshotService.delete(
                cls.OPERATION, admin_user_id, preview_token,
            )
            return None, '赌坊轮次或金币账本已变化，请重新预览后再执行'
        except Exception as exc:
            return None, f'赌坊金币校正失败，已整批回滚：{exc}'

        S1AdminScanSnapshotService.delete(cls.OPERATION, admin_user_id, preview_token)
        return {
            'executed_count': len(canonical_snapshot),
            'adventurer_count': len(adventurer_ids),
            'total_recovered': round(sum(item['overpay'] for item in canonical_snapshot), 2),
            'final_balances': {
                str(adventurer_id): round(float(balance), 2)
                for adventurer_id, balance in (balances or {}).items()
            },
        }, None
