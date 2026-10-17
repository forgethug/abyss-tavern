"""精确回收旧版低消费临时金重复增发，并提供条件恢复。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import secrets
from collections import defaultdict
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Currency,
    AdventurerS1CurrencyLog,
    AdventurerS1LowConsumptionOvercreditRepair,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
    S1CurrencyService,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)


class S1LowConsumptionOvercreditReclaimService:
    """按旧临时金流水逐笔配对原始金币收益，整批回收重复价值。"""

    SNAPSHOT_KIND = "low_consumption_overcredit_reclaim_v2"
    LEGACY_ACCRUAL_SOURCE_TYPE = "low_consumption_accrual"
    CURRENT_SPLIT_SOURCE_TYPE = "low_consumption_split"
    RECLAIM_SOURCE_TYPE = "low_consume_overcredit_reclaim"
    RESTORE_SOURCE_TYPE = "low_consume_overcredit_restore"
    RELATED_TYPE = "low_consume_overcredit"
    MAX_LEGACY_LOGS = 500
    MAX_GOLD_LEDGER_LOGS = 50000
    MATCH_WINDOW_SECONDS = 5
    LEGACY_RATE = 0.2
    LEGACY_CAP = 100.0
    LEGACY_ORIGINAL_SOURCE_TYPES = (
        "market_sell",
        "market_buy_order_sale",
        "expedition_reward",
        "task_reward",
    )
    OBSOLETE_REPAIR_SOURCE_TYPES = (
        "low_consume_cashout",
        "low_consume_restore",
    )
    POST_INCOME_CORRECTION_SOURCE_TYPES = (
        "admin_adjust",
        "bug_reclaim",
        "class_scroll_reclaim",
        "data_appeal_reclaim",
        "data_appeal_revert",
        "data_appeal_revert_sale",
        "gold_rail_sync",
    )
    STATE_KEYS = (
        "candidate_log_count",
        "candidate_count",
        "reclaim_total",
        "gold_reclaim_total",
        "temporary_reclaim_total",
        "new_split_total",
        "scan_truncated",
        "blocked_items",
        "items",
    )

    @staticmethod
    def _money(value) -> float:
        return round(float(value or 0), 2)

    @staticmethod
    def _timestamp(value) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _valid_batch_id(value) -> str | None:
        normalized = str(value or "").strip().lower()
        if len(normalized) != 32 or any(
            char not in "0123456789abcdef" for char in normalized
        ):
            return None
        return normalized

    @classmethod
    def _legacy_original_source(cls, log: AdventurerS1CurrencyLog) -> str | None:
        """只解析旧代码生成的固定审计模板，不对玩家文本做语义推断。"""
        description = str(log.description or "")
        marker = "（来源："
        prefix, found, suffix = description.rpartition(marker)
        if not found or not suffix.endswith("）"):
            return None
        source_type = suffix[:-1]
        if source_type not in cls.LEGACY_ORIGINAL_SOURCE_TYPES:
            return None
        if not prefix.startswith("金币收益的 ") or not prefix.endswith(
            "% 转为低消费临时金"
        ):
            return None
        return source_type

    @classmethod
    def _legacy_credit_matches(
        cls,
        legacy_log: AdventurerS1CurrencyLog,
        gold_log: AdventurerS1CurrencyLog,
    ) -> bool:
        gold_amount = cls._money(gold_log.change_amount)
        credit = cls._money(legacy_log.change_amount)
        pre_temporary = cls._money(
            cls._money(legacy_log.balance_after) - credit,
        )
        if (
            gold_amount <= 0
            or credit <= 0
            or pre_temporary < -0.001
            or pre_temporary > cls.LEGACY_CAP + 0.001
        ):
            return False
        requested = cls._money(gold_amount * cls.LEGACY_RATE)
        expected = cls._money(
            min(
                requested,
                max(0.0, cls.LEGACY_CAP - pre_temporary),
            )
        )
        return (
            expected == credit
            and cls._money(legacy_log.balance_after) <= cls.LEGACY_CAP
        )

    @staticmethod
    def _lineage_key(log: AdventurerS1CurrencyLog, source_type: str) -> tuple:
        return (
            int(log.adventurer_id),
            source_type,
            str(log.related_id) if log.related_id is not None else None,
            str(log.related_type) if log.related_type is not None else None,
        )

    @classmethod
    def _empty_analysis(cls) -> dict:
        return {
            "candidate_log_count": 0,
            "candidate_count": 0,
            "reclaim_total": 0.0,
            "gold_reclaim_total": 0.0,
            "temporary_reclaim_total": 0.0,
            "new_split_total": 0.0,
            "scan_truncated": False,
            "blocked_items": [],
            "items": [],
        }

    @classmethod
    def _analyze_state(cls) -> dict:
        result = cls._empty_analysis()
        legacy_logs = (
            AdventurerS1CurrencyLog.query.filter(
                AdventurerS1CurrencyLog.currency_code
                == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                AdventurerS1CurrencyLog.source_type == cls.LEGACY_ACCRUAL_SOURCE_TYPE,
                AdventurerS1CurrencyLog.change_amount > 0.001,
            )
            .order_by(
                AdventurerS1CurrencyLog.created_at.asc(),
                AdventurerS1CurrencyLog.id.asc(),
            )
            .limit(cls.MAX_LEGACY_LOGS + 1)
            .all()
        )
        result["candidate_log_count"] = min(len(legacy_logs), cls.MAX_LEGACY_LOGS)
        if len(legacy_logs) > cls.MAX_LEGACY_LOGS:
            result["scan_truncated"] = True
            result["blocked_items"].append(
                {
                    "code": "scan_truncated",
                    "message": (
                        f"旧增发流水超过单批上限 {cls.MAX_LEGACY_LOGS}，禁止不完整回收"
                    ),
                }
            )
            return result
        if not legacy_logs:
            return result

        adventurer_ids = sorted({int(log.adventurer_id) for log in legacy_logs})
        parsed_sources: dict[int, str] = {}
        for log in legacy_logs:
            source_type = cls._legacy_original_source(log)
            if source_type is None:
                result["blocked_items"].append(
                    {
                        "code": "unknown_legacy_description",
                        "legacy_accrual_log_id": int(log.id),
                        "adventurer_id": int(log.adventurer_id),
                        "message": (
                            "旧临时金流水不符合唯一固定模板，无法确认原始收益来源"
                        ),
                    }
                )
                continue
            parsed_sources[int(log.id)] = source_type

        min_created_at = min(log.created_at for log in legacy_logs)
        max_created_at = max(log.created_at for log in legacy_logs)
        gold_logs = (
            AdventurerS1CurrencyLog.query.filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code == "gold",
                AdventurerS1CurrencyLog.source_type.in_(
                    cls.LEGACY_ORIGINAL_SOURCE_TYPES,
                ),
                AdventurerS1CurrencyLog.change_amount > 0.001,
                AdventurerS1CurrencyLog.created_at
                >= min_created_at - timedelta(seconds=cls.MATCH_WINDOW_SECONDS),
                AdventurerS1CurrencyLog.created_at
                <= max_created_at + timedelta(seconds=cls.MATCH_WINDOW_SECONDS),
            )
            .order_by(
                AdventurerS1CurrencyLog.created_at.asc(),
                AdventurerS1CurrencyLog.id.asc(),
            )
            .all()
        )
        gold_by_key: dict[tuple, list[AdventurerS1CurrencyLog]] = defaultdict(list)
        for gold_log in gold_logs:
            gold_by_key[cls._lineage_key(gold_log, gold_log.source_type)].append(
                gold_log
            )

        used_gold_log_ids: set[int] = set()
        matched_entries = []
        for legacy_log in legacy_logs:
            source_type = parsed_sources.get(int(legacy_log.id))
            if source_type is None:
                continue
            key = cls._lineage_key(legacy_log, source_type)
            ranked = []
            for gold_log in gold_by_key.get(key, []):
                if int(gold_log.id) in used_gold_log_ids:
                    continue
                delta_seconds = abs(
                    (legacy_log.created_at - gold_log.created_at).total_seconds(),
                )
                if delta_seconds > cls.MATCH_WINDOW_SECONDS:
                    continue
                if not cls._legacy_credit_matches(legacy_log, gold_log):
                    continue
                ranked.append((delta_seconds, int(gold_log.id), gold_log))
            ranked.sort(key=lambda row: (row[0], row[1]))
            if not ranked:
                result["blocked_items"].append(
                    {
                        "code": "original_gold_log_missing",
                        "legacy_accrual_log_id": int(legacy_log.id),
                        "adventurer_id": int(legacy_log.adventurer_id),
                        "message": (
                            "找不到金额、来源、关联对象和时间均一致的原始金币收益流水"
                        ),
                    }
                )
                continue
            if len(ranked) > 1:
                result["blocked_items"].append(
                    {
                        "code": "original_gold_log_ambiguous",
                        "legacy_accrual_log_id": int(legacy_log.id),
                        "adventurer_id": int(legacy_log.adventurer_id),
                        "candidate_gold_log_ids": [row[1] for row in ranked],
                        "message": "存在多条可匹配的原始金币流水，无法唯一配对",
                    }
                )
                continue
            gold_log = ranked[0][2]
            used_gold_log_ids.add(int(gold_log.id))
            pre_income_gold = cls._money(
                cls._money(gold_log.balance_after) - cls._money(gold_log.change_amount),
            )
            was_in_debt = pre_income_gold < -0.001
            matched_entries.append(
                {
                    "legacy_accrual_log_id": int(legacy_log.id),
                    "paired_gold_log_id": int(gold_log.id),
                    "adventurer_id": int(legacy_log.adventurer_id),
                    "original_source_type": source_type,
                    "related_id": legacy_log.related_id,
                    "related_type": legacy_log.related_type,
                    "legacy_created_at": cls._timestamp(legacy_log.created_at),
                    "gold_created_at": cls._timestamp(gold_log.created_at),
                    "original_gold_income": cls._money(gold_log.change_amount),
                    "pre_income_gold": pre_income_gold,
                    "was_in_debt": was_in_debt,
                    "overcredit_amount": cls._money(legacy_log.change_amount),
                    "recovery_currency_code": (
                        "gold"
                        if was_in_debt
                        else S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE
                    ),
                }
            )

        traceable_gold_balance_by_adventurer: dict[int, float] = {}
        if matched_entries:
            paired_ids_by_adventurer: dict[int, set[int]] = defaultdict(set)
            for entry in matched_entries:
                paired_ids_by_adventurer[int(entry["adventurer_id"])].add(
                    int(entry["paired_gold_log_id"]),
                )
            min_paired_gold_log_id = min(
                int(entry["paired_gold_log_id"]) for entry in matched_entries
            )
            gold_ledger_logs = (
                AdventurerS1CurrencyLog.query.filter(
                    AdventurerS1CurrencyLog.adventurer_id.in_(
                        list(paired_ids_by_adventurer),
                    ),
                    AdventurerS1CurrencyLog.currency_code == "gold",
                    AdventurerS1CurrencyLog.id >= min_paired_gold_log_id,
                )
                .order_by(
                    AdventurerS1CurrencyLog.adventurer_id.asc(),
                    AdventurerS1CurrencyLog.id.asc(),
                )
                .limit(cls.MAX_GOLD_LEDGER_LOGS + 1)
                .all()
            )
            if len(gold_ledger_logs) > cls.MAX_GOLD_LEDGER_LOGS:
                result["scan_truncated"] = True
                result["blocked_items"].append(
                    {
                        "code": "gold_ledger_scan_truncated",
                        "message": (
                            "候选角色的金币流水超过安全扫描上限 "
                            f"{cls.MAX_GOLD_LEDGER_LOGS}，禁止不完整回收"
                        ),
                    }
                )
                return result
            gold_ledger_by_adventurer: dict[
                int, list[AdventurerS1CurrencyLog]
            ] = defaultdict(list)
            for log in gold_ledger_logs:
                gold_ledger_by_adventurer[int(log.adventurer_id)].append(log)
            for adventurer_id, paired_ids in paired_ids_by_adventurer.items():
                logs = gold_ledger_by_adventurer.get(adventurer_id, [])
                start_index = next(
                    (
                        index
                        for index, log in enumerate(logs)
                        if int(log.id) in paired_ids
                    ),
                    None,
                )
                if start_index is None:
                    result["blocked_items"].append(
                        {
                            "code": "gold_ledger_lineage_missing",
                            "adventurer_id": adventurer_id,
                            "message": "金币账本缺少已配对的原始收益流水",
                        }
                    )
                    continue
                first = logs[start_index]
                running = cls._money(
                    cls._money(first.balance_after)
                    - cls._money(first.change_amount),
                )
                continuity_ok = True
                for log in logs[start_index:]:
                    running = cls._money(
                        running + cls._money(log.change_amount),
                    )
                    if running != cls._money(log.balance_after):
                        continuity_ok = False
                        break
                if not continuity_ok:
                    result["blocked_items"].append(
                        {
                            "code": "gold_ledger_discontinuous",
                            "adventurer_id": adventurer_id,
                            "currency_log_id": int(log.id),
                            "message": (
                                "原收益后的金币流水存在无日志余额跃迁，无法排除重复价值"
                                "已被静默纠正"
                            ),
                        }
                    )
                    continue
                traceable_gold_balance_by_adventurer[adventurer_id] = running

        correction_logs = (
            AdventurerS1CurrencyLog.query.filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code == "gold",
                AdventurerS1CurrencyLog.change_amount < -0.001,
                AdventurerS1CurrencyLog.source_type.in_(
                    cls.POST_INCOME_CORRECTION_SOURCE_TYPES,
                ),
                AdventurerS1CurrencyLog.created_at >= min_created_at,
            )
            .order_by(AdventurerS1CurrencyLog.id.asc())
            .all()
        )
        corrections_by_adventurer: dict[int, list[AdventurerS1CurrencyLog]] = (
            defaultdict(list)
        )
        for correction_log in correction_logs:
            corrections_by_adventurer[int(correction_log.adventurer_id)].append(
                correction_log,
            )
        for entry in matched_entries:
            later_corrections = [
                log
                for log in corrections_by_adventurer[int(entry["adventurer_id"])]
                if int(log.id) > int(entry["paired_gold_log_id"])
            ]
            if not later_corrections:
                continue
            result["blocked_items"].append(
                {
                    "code": "post_income_correction_requires_manual_trace",
                    "legacy_accrual_log_id": entry["legacy_accrual_log_id"],
                    "paired_gold_log_id": entry["paired_gold_log_id"],
                    "adventurer_id": entry["adventurer_id"],
                    "correction_gold_log_ids": [
                        int(log.id) for log in later_corrections
                    ],
                    "message": (
                        "原收益后存在管理员、申诉或历史修复扣款，无法证明该扣款"
                        "是否已经纠正重复价值，禁止自动回收"
                    ),
                }
            )

        entries_for_classification: dict[int, list[dict]] = defaultdict(list)
        for entry in matched_entries:
            entries_for_classification[int(entry["adventurer_id"])].append(entry)
        for adventurer_id, entries in entries_for_classification.items():
            if len({bool(entry["was_in_debt"]) for entry in entries}) > 1:
                result["blocked_items"].append(
                    {
                        "code": "mixed_historical_debt_classification",
                        "adventurer_id": adventurer_id,
                        "legacy_accrual_log_ids": [
                            int(entry["legacy_accrual_log_id"])
                            for entry in entries
                        ],
                        "message": (
                            "同一冒险者的旧收益跨越负债边界，前笔纠正会改变后笔分类，"
                            "禁止按错误账本逐笔回收"
                        ),
                    }
                )

        configured_income_sources = tuple(
            str(value).strip()
            for value in current_app.config.get(
                "ADVENTURER_S1_LOW_CONSUMPTION_INCOME_SOURCE_TYPES",
                (),
            )
            if str(value).strip()
        )
        if not configured_income_sources:
            configured_income_sources = tuple(
                S1CurrencyService.LOW_CONSUMPTION_INCOME_SOURCE_LABELS
            )
        if configured_income_sources:
            later_eligible_gold_logs = (
                AdventurerS1CurrencyLog.query.filter(
                    AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                    AdventurerS1CurrencyLog.currency_code == "gold",
                    AdventurerS1CurrencyLog.source_type.in_(
                        configured_income_sources,
                    ),
                    AdventurerS1CurrencyLog.change_amount > 0.001,
                    AdventurerS1CurrencyLog.created_at >= min_created_at,
                )
                .order_by(AdventurerS1CurrencyLog.id.asc())
                .all()
            )
            eligible_gold_by_adventurer: dict[
                int, list[AdventurerS1CurrencyLog]
            ] = defaultdict(list)
            for gold_log in later_eligible_gold_logs:
                eligible_gold_by_adventurer[int(gold_log.adventurer_id)].append(
                    gold_log,
                )
            for adventurer_id, entries in entries_for_classification.items():
                debt_entries = [entry for entry in entries if entry["was_in_debt"]]
                if debt_entries and len(debt_entries) == len(entries):
                    debt_status_changed = []
                    for gold_log in eligible_gold_by_adventurer[adventurer_id]:
                        cumulative_reclaim = cls._money(sum(
                            entry["overcredit_amount"]
                            for entry in debt_entries
                            if int(entry["paired_gold_log_id"]) < int(gold_log.id)
                        ))
                        if cumulative_reclaim <= 0:
                            continue
                        actual_pre_income_gold = cls._money(
                            cls._money(gold_log.balance_after)
                            - cls._money(gold_log.change_amount),
                        )
                        corrected_pre_income_gold = cls._money(
                            actual_pre_income_gold - cumulative_reclaim,
                        )
                        if (
                            actual_pre_income_gold >= -0.001
                            and corrected_pre_income_gold < -0.001
                        ):
                            debt_status_changed.append({
                                "gold_log_id": int(gold_log.id),
                                "actual_pre_income_gold": actual_pre_income_gold,
                                "corrected_pre_income_gold": corrected_pre_income_gold,
                            })
                    if debt_status_changed:
                        result["blocked_items"].append(
                            {
                                "code": "later_income_debt_status_changes_after_reclaim",
                                "adventurer_id": adventurer_id,
                                "later_income_logs": debt_status_changed,
                                "message": (
                                    "回收旧负债收益的重复金币后，后续合格收益本应仍处于"
                                    "负债并发生分流；未完整重放前禁止自动回收"
                                ),
                            }
                        )

                nondebt_entries = [
                    entry for entry in entries if not entry["was_in_debt"]
                ]
                if not nondebt_entries:
                    continue
                first_nondebt_gold_log_id = min(
                    int(entry["paired_gold_log_id"])
                    for entry in nondebt_entries
                )
                potentially_clipped = []
                for gold_log in eligible_gold_by_adventurer[adventurer_id]:
                    if int(gold_log.id) <= first_nondebt_gold_log_id:
                        continue
                    pre_income_gold = cls._money(
                        cls._money(gold_log.balance_after)
                        - cls._money(gold_log.change_amount),
                    )
                    if pre_income_gold < -0.001:
                        potentially_clipped.append(gold_log)
                if potentially_clipped:
                    result["blocked_items"].append(
                        {
                            "code": "later_debt_income_may_be_cap_clipped",
                            "adventurer_id": adventurer_id,
                            "first_nondebt_gold_log_id": first_nondebt_gold_log_id,
                            "later_gold_log_ids": [
                                int(log.id) for log in potentially_clipped
                            ],
                            "message": (
                                "非负债误发临时金后又出现负债合格收益，旧余额可能挤占"
                                "临时金上限，禁止在未完整重放账本时自动回收"
                            ),
                        }
                    )

        prior_repairs = (
            AdventurerS1LowConsumptionOvercreditRepair.query.filter(
                AdventurerS1LowConsumptionOvercreditRepair.legacy_accrual_log_id.in_(
                    [int(log.id) for log in legacy_logs],
                ),
            )
            .order_by(AdventurerS1LowConsumptionOvercreditRepair.id.asc())
            .all()
        )
        if prior_repairs:
            result["blocked_items"].append(
                {
                    "code": "existing_repair_receipts",
                    "adventurer_ids": sorted({
                        int(row.adventurer_id) for row in prior_repairs
                    }),
                    "legacy_accrual_log_ids": [
                        int(row.legacy_accrual_log_id) for row in prior_repairs
                    ],
                    "message": "部分旧流水已有修复或恢复凭证，禁止新批次重复处理",
                }
            )

        obsolete_logs = (
            AdventurerS1CurrencyLog.query.filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.source_type.in_(
                    cls.OBSOLETE_REPAIR_SOURCE_TYPES,
                ),
            )
            .order_by(AdventurerS1CurrencyLog.id.asc())
            .all()
        )
        if obsolete_logs:
            result["blocked_items"].append(
                {
                    "code": "obsolete_cashout_history",
                    "adventurer_ids": sorted({
                        int(log.adventurer_id) for log in obsolete_logs
                    }),
                    "currency_log_ids": [int(log.id) for log in obsolete_logs],
                    "message": "发现旧版临时金转现金流水，必须人工核账，禁止自动回收",
                }
            )

        temporary_logs = (
            AdventurerS1CurrencyLog.query.filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code
                == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
            )
            .order_by(
                AdventurerS1CurrencyLog.created_at.asc(),
                AdventurerS1CurrencyLog.id.asc(),
            )
            .all()
        )
        legacy_log_ids = {int(log.id) for log in legacy_logs}
        new_split_by_adventurer: dict[int, float] = defaultdict(float)
        temporary_logs_by_adventurer: dict[
            int, list[AdventurerS1CurrencyLog]
        ] = defaultdict(list)
        unknown_positive_by_adventurer: dict[
            int, list[AdventurerS1CurrencyLog]
        ] = defaultdict(list)
        for log in temporary_logs:
            adventurer_id = int(log.adventurer_id)
            temporary_logs_by_adventurer[adventurer_id].append(log)
            if int(log.id) in legacy_log_ids:
                continue
            if (
                log.source_type == cls.CURRENT_SPLIT_SOURCE_TYPE
                and cls._money(log.change_amount) > 0
            ):
                new_split_by_adventurer[adventurer_id] += cls._money(
                    log.change_amount,
                )
                continue
            if cls._money(log.change_amount) > 0:
                unknown_positive_by_adventurer[adventurer_id].append(log)
        for adventurer_id, logs in unknown_positive_by_adventurer.items():
            result["blocked_items"].append(
                {
                    "code": "unknown_positive_temporary_lineage",
                    "adventurer_id": adventurer_id,
                    "currency_log_ids": [
                        int(log.id) for log in logs
                    ],
                    "message": (
                        "临时金存在旧增发与新版分流之外的正向来源，禁止自动判断归属"
                    ),
                }
            )

        traceable_temporary_balance_by_adventurer: dict[int, float] = {}
        for adventurer_id, logs in temporary_logs_by_adventurer.items():
            if not logs:
                continue
            first = logs[0]
            running = cls._money(
                cls._money(first.balance_after) - cls._money(first.change_amount),
            )
            continuity_ok = abs(running) <= 0.001
            for log in logs:
                running = cls._money(running + cls._money(log.change_amount))
                if running != cls._money(log.balance_after):
                    continuity_ok = False
                    break
            if not continuity_ok:
                result["blocked_items"].append(
                    {
                        "code": "temporary_ledger_discontinuous",
                        "adventurer_id": adventurer_id,
                        "message": "临时金流水无法从零连续重放到当前余额",
                    }
                )
                continue
            traceable_temporary_balance_by_adventurer[adventurer_id] = running

        adventurers = (
            AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids))
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        adventurer_map = {int(row.id): row for row in adventurers}
        currencies = (
            AdventurerS1Currency.query.filter(
                AdventurerS1Currency.adventurer_id.in_(adventurer_ids),
                AdventurerS1Currency.currency_code.in_(
                    [
                        "gold",
                        S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                    ]
                ),
            )
            .order_by(
                AdventurerS1Currency.adventurer_id.asc(),
                AdventurerS1Currency.currency_code.asc(),
            )
            .all()
        )
        currency_map = {
            (int(row.adventurer_id), row.currency_code): row for row in currencies
        }
        frontier_rows = (
            AdventurerS1CurrencyLog.query.with_entities(
                AdventurerS1CurrencyLog.adventurer_id,
                AdventurerS1CurrencyLog.currency_code,
                func.max(AdventurerS1CurrencyLog.id),
            )
            .filter(
                AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                AdventurerS1CurrencyLog.currency_code.in_(
                    [
                        "gold",
                        S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                    ]
                ),
            )
            .group_by(
                AdventurerS1CurrencyLog.adventurer_id,
                AdventurerS1CurrencyLog.currency_code,
            )
            .all()
        )
        frontier_map = {
            (int(adventurer_id), currency_code): int(max_id or 0)
            for adventurer_id, currency_code, max_id in frontier_rows
        }

        legacy_total_by_adventurer: dict[int, float] = defaultdict(float)
        for log in legacy_logs:
            legacy_total_by_adventurer[int(log.adventurer_id)] += cls._money(
                log.change_amount,
            )
        entries_by_adventurer: dict[int, list[dict]] = defaultdict(list)
        for entry in matched_entries:
            entries_by_adventurer[int(entry["adventurer_id"])].append(entry)

        preblocked_adventurer_ids = set()
        for blocked in result["blocked_items"]:
            if blocked.get("adventurer_id") is not None:
                preblocked_adventurer_ids.add(int(blocked["adventurer_id"]))
            preblocked_adventurer_ids.update(
                int(value) for value in (blocked.get("adventurer_ids") or [])
            )

        items = []
        for adventurer_id in adventurer_ids:
            if adventurer_id in preblocked_adventurer_ids:
                continue
            adventurer = adventurer_map.get(adventurer_id)
            gold_row = currency_map.get((adventurer_id, "gold"))
            temporary_row = currency_map.get(
                (
                    adventurer_id,
                    S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                )
            )
            if adventurer is None or gold_row is None or temporary_row is None:
                result["blocked_items"].append(
                    {
                        "code": "balance_row_missing",
                        "adventurer_id": adventurer_id,
                        "message": "冒险者、金币或临时金余额行缺失",
                    }
                )
                continue
            gold_balance = cls._money(gold_row.amount)
            temporary_balance = cls._money(temporary_row.amount)
            if (
                temporary_balance < -0.001
                or temporary_balance > cls.LEGACY_CAP + 0.001
            ):
                result["blocked_items"].append(
                    {
                        "code": "temporary_balance_out_of_bounds",
                        "adventurer_id": adventurer_id,
                        "temporary_balance": temporary_balance,
                        "message": "当前临时金余额超出 0 至 100 的合法范围",
                    }
                )
                continue
            if cls._money(adventurer.gold) != gold_balance:
                result["blocked_items"].append(
                    {
                        "code": "gold_rail_mismatch",
                        "adventurer_id": adventurer_id,
                        "main_gold_balance": cls._money(adventurer.gold),
                        "currency_gold_balance": gold_balance,
                        "message": "主表金币与货币账本不一致",
                    }
                )
                continue
            traceable_gold = traceable_gold_balance_by_adventurer.get(adventurer_id)
            if traceable_gold is None or gold_balance != traceable_gold:
                result["blocked_items"].append(
                    {
                        "code": "gold_balance_not_fully_traceable",
                        "adventurer_id": adventurer_id,
                        "gold_balance": gold_balance,
                        "traceable_balance": traceable_gold,
                        "message": "当前金币余额与原收益后的完整流水重放结果不一致",
                    }
                )
                continue
            traceable_temporary = traceable_temporary_balance_by_adventurer.get(
                adventurer_id,
            )
            if (
                traceable_temporary is None
                or temporary_balance != traceable_temporary
            ):
                result["blocked_items"].append(
                    {
                        "code": "temporary_balance_not_fully_traceable",
                        "adventurer_id": adventurer_id,
                        "temporary_balance": temporary_balance,
                        "traceable_balance": traceable_temporary,
                        "message": "当前临时金余额与完整流水重放结果不一致",
                    }
                )
                continue
            entries = sorted(
                entries_by_adventurer.get(adventurer_id, []),
                key=lambda row: row["legacy_accrual_log_id"],
            )
            if cls._money(
                sum(row["overcredit_amount"] for row in entries)
            ) != cls._money(
                legacy_total_by_adventurer[adventurer_id],
            ):
                result["blocked_items"].append(
                    {
                        "code": "lineage_incomplete",
                        "adventurer_id": adventurer_id,
                        "message": "该冒险者存在未能唯一配对的旧增发流水",
                    }
                )
                continue
            all_were_in_debt = all(row["was_in_debt"] for row in entries)
            if not all_were_in_debt:
                if new_split_by_adventurer[adventurer_id] > 0.001:
                    result["blocked_items"].append(
                        {
                            "code": "nondebt_overcredit_mixed_with_new_split",
                            "adventurer_id": adventurer_id,
                            "message": (
                                "非负债旧误发与新版正确分流共池，无法证明消费及上限归属"
                            ),
                        }
                    )
                    continue
                if temporary_balance != cls._money(
                    legacy_total_by_adventurer[adventurer_id]
                ):
                    result["blocked_items"].append(
                        {
                            "code": "nondebt_overcredit_was_spent",
                            "adventurer_id": adventurer_id,
                            "temporary_balance": temporary_balance,
                            "legacy_accrual_total": cls._money(
                                legacy_total_by_adventurer[adventurer_id]
                            ),
                            "message": (
                                "非负债误发临时金已发生消费，当前版本不猜测逐笔消费归属"
                            ),
                        }
                    )
                    continue
            gold_reclaim = (
                cls._money(legacy_total_by_adventurer[adventurer_id])
                if all_were_in_debt
                else 0.0
            )
            temporary_reclaim = (
                0.0
                if all_were_in_debt
                else cls._money(legacy_total_by_adventurer[adventurer_id])
            )
            items.append(
                {
                    "adventurer_id": adventurer_id,
                    "adventurer_name": str(adventurer.name or ""),
                    "owner_id": int(adventurer.owner_id)
                    if adventurer.owner_id is not None
                    else None,
                    "main_gold_balance": cls._money(adventurer.gold),
                    "gold_currency_id": int(gold_row.id),
                    "gold_balance": gold_balance,
                    "gold_updated_at": cls._timestamp(gold_row.updated_at),
                    "gold_log_frontier_id": frontier_map.get(
                        (adventurer_id, "gold"), 0
                    ),
                    "temporary_currency_id": int(temporary_row.id),
                    "temporary_balance": temporary_balance,
                    "temporary_updated_at": cls._timestamp(temporary_row.updated_at),
                    "temporary_log_frontier_id": frontier_map.get(
                        (
                            adventurer_id,
                            S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                        ),
                        0,
                    ),
                    "new_split_total": cls._money(
                        new_split_by_adventurer[adventurer_id]
                    ),
                    "legacy_accrual_total": cls._money(
                        legacy_total_by_adventurer[adventurer_id]
                    ),
                    "gold_reclaim_amount": gold_reclaim,
                    "temporary_reclaim_amount": temporary_reclaim,
                    "gold_after": cls._money(gold_balance - gold_reclaim),
                    "temporary_after": cls._money(
                        temporary_balance - temporary_reclaim
                    ),
                    "entries": entries,
                }
            )

        result["items"] = items
        result["candidate_count"] = len(items)
        result["gold_reclaim_total"] = cls._money(
            sum(item["gold_reclaim_amount"] for item in items)
        )
        result["temporary_reclaim_total"] = cls._money(
            sum(item["temporary_reclaim_amount"] for item in items)
        )
        result["reclaim_total"] = cls._money(
            result["gold_reclaim_total"] + result["temporary_reclaim_total"],
        )
        result["new_split_total"] = cls._money(
            sum(item["new_split_total"] for item in items)
        )
        return result

    @classmethod
    def _state_view(cls, value: dict) -> dict:
        return {key: value.get(key) for key in cls.STATE_KEYS}

    @classmethod
    def preview(cls, admin_user_id: int):
        analysis = cls._analyze_state()
        result = {
            **analysis,
            "batch_id": None,
            "snapshot_token": None,
        }
        if (
            analysis["items"]
            and not analysis["scan_truncated"]
        ):
            result["batch_id"] = secrets.token_hex(16)
            result["snapshot_token"] = S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_KIND,
                admin_user_id,
                result,
                allow_signed_fallback=True,
            )
        return result, None

    @classmethod
    def _expected_receipts(cls, candidates: list[dict]) -> dict[int, dict]:
        expected = {}
        for candidate in candidates:
            for entry in candidate.get("entries") or []:
                expected[int(entry["legacy_accrual_log_id"])] = {
                    **entry,
                    "adventurer_name": candidate.get("adventurer_name") or "",
                }
        return expected

    @classmethod
    def _executed_result(cls, candidates: list[dict], batch_id: str):
        expected = cls._expected_receipts(candidates)
        receipts = (
            AdventurerS1LowConsumptionOvercreditRepair.query.filter_by(
                batch_id=batch_id
            )
            .order_by(
                AdventurerS1LowConsumptionOvercreditRepair.legacy_accrual_log_id.asc()
            )
            .all()
        )
        if not receipts:
            return None, None
        if {int(row.legacy_accrual_log_id) for row in receipts} != set(expected):
            return None, "该批次修复凭证不完整或与预览不一致，禁止重放"
        if any(row.status == "restored" for row in receipts):
            if all(row.status == "restored" for row in receipts):
                return None, "该批次已恢复，禁止再次执行旧预览"
            return None, "该批次存在部分恢复凭证，必须人工核查"
        if any(row.status != "executed" for row in receipts):
            return None, "该批次修复凭证状态异常，禁止重放"
        for receipt in receipts:
            item = expected[int(receipt.legacy_accrual_log_id)]
            if (
                int(receipt.paired_gold_log_id) != int(item["paired_gold_log_id"])
                or int(receipt.adventurer_id) != int(item["adventurer_id"])
                or receipt.recovery_currency_code != item["recovery_currency_code"]
                or cls._money(receipt.reclaim_amount)
                != cls._money(item["overcredit_amount"])
            ):
                return None, "该批次修复凭证与逐笔收益谱系不一致，禁止重放"
        receipt_logs = (
            AdventurerS1CurrencyLog.query.filter_by(
                source_type=cls.RECLAIM_SOURCE_TYPE,
                related_id=batch_id,
                related_type=cls.RELATED_TYPE,
            )
            .order_by(
                AdventurerS1CurrencyLog.adventurer_id.asc(),
                AdventurerS1CurrencyLog.currency_code.asc(),
            )
            .all()
        )
        expected_groups = {
            (int(row.adventurer_id), row.recovery_currency_code) for row in receipts
        }
        log_groups = {
            (int(log.adventurer_id), log.currency_code) for log in receipt_logs
        }
        if expected_groups != log_groups or len(receipt_logs) != len(expected_groups):
            return None, "该批次回收余额流水不完整，禁止按幂等成功返回"
        return {
            "batch_id": batch_id,
            "reclaimed_count": len({int(row.adventurer_id) for row in receipts}),
            "reclaimed_line_count": len(receipts),
            "reclaimed_total": cls._money(sum(row.reclaim_amount for row in receipts)),
            "gold_reclaimed_total": cls._money(
                sum(
                    row.reclaim_amount
                    for row in receipts
                    if row.recovery_currency_code == "gold"
                )
            ),
            "temporary_reclaimed_total": cls._money(
                sum(
                    row.reclaim_amount
                    for row in receipts
                    if row.recovery_currency_code
                    == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE
                )
            ),
            "already_executed": True,
            "items": [],
        }, None

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        preview = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_KIND,
            admin_user_id,
            preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, "预览快照已过期，请重新预览后再执行"
        if preview.get("scan_truncated"):
            return None, "预览扫描被截断，禁止执行不完整回收"
        candidates = preview.get("items") or []
        if not candidates:
            return {
                "batch_id": None,
                "reclaimed_count": 0,
                "reclaimed_line_count": 0,
                "reclaimed_total": 0.0,
                "gold_reclaimed_total": 0.0,
                "temporary_reclaimed_total": 0.0,
                "blocked_item_count": len(preview.get("blocked_items") or []),
                "already_executed": False,
                "items": [],
            }, None
        batch_id = cls._valid_batch_id(preview.get("batch_id"))
        if batch_id is None:
            return None, "预览快照缺少有效批次标识，请重新预览"
        executed_result, executed_err = cls._executed_result(candidates, batch_id)
        if executed_err or executed_result is not None:
            return executed_result, executed_err

        candidate_by_id = {
            int(candidate["adventurer_id"]): candidate for candidate in candidates
        }
        adventurer_ids = sorted(candidate_by_id)
        owner_ids = set()
        executed_items = []
        try:
            with db_manager.transaction():
                adventurers = (
                    AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids))
                    .order_by(AdventurerS1.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                adventurer_map = {int(row.id): row for row in adventurers}
                currencies = (
                    AdventurerS1Currency.query.filter(
                        AdventurerS1Currency.adventurer_id.in_(adventurer_ids),
                        AdventurerS1Currency.currency_code.in_(
                            [
                                "gold",
                                S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                            ]
                        ),
                    )
                    .order_by(
                        AdventurerS1Currency.adventurer_id.asc(),
                        AdventurerS1Currency.currency_code.asc(),
                    )
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                currency_map = {
                    (int(row.adventurer_id), row.currency_code): row
                    for row in currencies
                }
                if (
                    len(adventurer_map) != len(adventurer_ids)
                    or len(currency_map) != len(adventurer_ids) * 2
                ):
                    raise RuntimeError("预览中的冒险者或余额行已缺失")

                actual = cls._analyze_state()
                if cls._state_view(actual) != cls._state_view(preview):
                    raise RuntimeError(
                        "收益谱系、余额或流水水位在预览后已变化，请重新预览"
                    )

                group_logs: dict[tuple[int, str], AdventurerS1CurrencyLog] = {}
                group_before_after: dict[tuple[int, str], tuple[float, float, int]] = {}
                for adventurer_id in adventurer_ids:
                    candidate = candidate_by_id[adventurer_id]
                    adventurer = adventurer_map[adventurer_id]
                    gold_row = currency_map[(adventurer_id, "gold")]
                    temporary_row = currency_map[
                        (
                            adventurer_id,
                            S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                        )
                    ]
                    if (
                        int(gold_row.id) != int(candidate["gold_currency_id"])
                        or int(temporary_row.id)
                        != int(candidate["temporary_currency_id"])
                        or cls._money(adventurer.gold)
                        != cls._money(candidate["main_gold_balance"])
                        or cls._money(gold_row.amount)
                        != cls._money(candidate["gold_balance"])
                        or cls._money(temporary_row.amount)
                        != cls._money(candidate["temporary_balance"])
                        or cls._timestamp(gold_row.updated_at)
                        != candidate.get("gold_updated_at")
                        or cls._timestamp(temporary_row.updated_at)
                        != candidate.get("temporary_updated_at")
                    ):
                        raise RuntimeError(
                            f"冒险者 #{adventurer_id} 的余额在预览后已变化，请重新预览",
                        )
                    for currency_code, amount, frontier_key in (
                        (
                            "gold",
                            cls._money(candidate["gold_reclaim_amount"]),
                            "gold_log_frontier_id",
                        ),
                        (
                            S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                            cls._money(candidate["temporary_reclaim_amount"]),
                            "temporary_log_frontier_id",
                        ),
                    ):
                        if amount <= 0:
                            continue
                        row = currency_map[(adventurer_id, currency_code)]
                        before = cls._money(row.amount)
                        after = cls._money(before - amount)
                        if (
                            currency_code
                            == S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE
                            and after < -0.001
                        ):
                            raise RuntimeError(
                                f"冒险者 #{adventurer_id} 的可追溯临时金不足"
                            )
                        row.amount = after
                        if currency_code == "gold":
                            adventurer.gold = after
                        description = (
                            "回收旧版低消费临时金重复增发：负债收益曾多入普通金币"
                            if currency_code == "gold"
                            else "回收非负债收益误发的低消费临时金"
                        )
                        log = S1CurrencyService._build_log(
                            adventurer_id,
                            currency_code,
                            -amount,
                            after,
                            cls.RECLAIM_SOURCE_TYPE,
                            batch_id,
                            cls.RELATED_TYPE,
                            description,
                        )
                        group_logs[(adventurer_id, currency_code)] = log
                        group_before_after[(adventurer_id, currency_code)] = (
                            before,
                            after,
                            int(candidate[frontier_key]),
                        )
                    if adventurer.owner_id is not None:
                        owner_ids.add(int(adventurer.owner_id))
                    executed_items.append(
                        {
                            "adventurer_id": adventurer_id,
                            "adventurer_name": adventurer.name,
                            "gold_reclaimed": cls._money(
                                candidate["gold_reclaim_amount"]
                            ),
                            "temporary_reclaimed": cls._money(
                                candidate["temporary_reclaim_amount"]
                            ),
                            "gold_after": cls._money(gold_row.amount),
                            "temporary_after": cls._money(temporary_row.amount),
                        }
                    )
                ok, log_err = db_manager.add_all(list(group_logs.values()), flush=True)
                if not ok:
                    raise RuntimeError(log_err)

                receipts = []
                for candidate in candidates:
                    for entry in candidate.get("entries") or []:
                        currency_code = entry["recovery_currency_code"]
                        group_key = (int(candidate["adventurer_id"]), currency_code)
                        before, after, frontier_before = group_before_after[group_key]
                        receipts.append(
                            AdventurerS1LowConsumptionOvercreditRepair(
                                batch_id=batch_id,
                                legacy_accrual_log_id=int(
                                    entry["legacy_accrual_log_id"]
                                ),
                                paired_gold_log_id=int(entry["paired_gold_log_id"]),
                                adventurer_id=int(candidate["adventurer_id"]),
                                original_source_type=entry["original_source_type"],
                                pre_income_gold=cls._money(entry["pre_income_gold"]),
                                was_in_debt=bool(entry["was_in_debt"]),
                                recovery_currency_code=currency_code,
                                reclaim_amount=cls._money(entry["overcredit_amount"]),
                                currency_balance_before=before,
                                currency_balance_after=after,
                                currency_log_frontier_before=frontier_before,
                                reclaim_currency_log_id=int(group_logs[group_key].id),
                                admin_user_id=int(admin_user_id),
                                status="executed",
                            )
                        )
                ok, receipt_err = db_manager.add_all(receipts, flush=True)
                if not ok:
                    raise RuntimeError(receipt_err)
        except Exception as exc:  # noqa: BLE001
            executed_result, executed_err = cls._executed_result(candidates, batch_id)
            if executed_err or executed_result is not None:
                return executed_result, executed_err
            claimed_elsewhere = AdventurerS1LowConsumptionOvercreditRepair.query.filter(
                AdventurerS1LowConsumptionOvercreditRepair.legacy_accrual_log_id.in_(
                    list(cls._expected_receipts(candidates)),
                ),
            ).count()
            if claimed_elsewhere:
                return None, "部分旧增发已由另一修复批次处理，请重新预览"
            return None, f"旧临时金重复增发回收失败，已整批回滚：{exc}"

        cls._invalidate_activity(owner_ids)
        return {
            "batch_id": batch_id,
            "reclaimed_count": len(executed_items),
            "reclaimed_line_count": sum(
                len(item.get("entries") or []) for item in candidates
            ),
            "reclaimed_total": cls._money(
                sum(
                    item["gold_reclaimed"] + item["temporary_reclaimed"]
                    for item in executed_items
                )
            ),
            "gold_reclaimed_total": cls._money(
                sum(item["gold_reclaimed"] for item in executed_items)
            ),
            "temporary_reclaimed_total": cls._money(
                sum(item["temporary_reclaimed"] for item in executed_items)
            ),
            "blocked_item_count": len(preview.get("blocked_items") or []),
            "already_executed": False,
            "items": executed_items,
        }, None

    @classmethod
    def restore(cls, admin_user_id: int, batch_id: str):
        normalized_batch_id = cls._valid_batch_id(batch_id)
        if normalized_batch_id is None:
            return None, "batch_id 格式无效"
        receipts = (
            AdventurerS1LowConsumptionOvercreditRepair.query.filter_by(
                batch_id=normalized_batch_id
            )
            .order_by(
                AdventurerS1LowConsumptionOvercreditRepair.adventurer_id.asc(),
                AdventurerS1LowConsumptionOvercreditRepair.recovery_currency_code.asc(),
                AdventurerS1LowConsumptionOvercreditRepair.id.asc(),
            )
            .all()
        )
        if not receipts:
            return None, "找不到该批次的逐笔回收凭证"
        if all(row.status == "restored" for row in receipts):
            if any(row.restore_currency_log_id is None for row in receipts):
                return None, "该批次恢复凭证不完整，必须人工核查"
            return {
                "batch_id": normalized_batch_id,
                "restored_count": 0,
                "restored_line_count": 0,
                "restored_total": 0.0,
                "already_restored": True,
                "items": [],
            }, None
        if any(row.status != "executed" for row in receipts):
            return None, "该批次存在部分恢复或异常状态，禁止自动恢复"

        groups: dict[tuple[int, str], list] = defaultdict(list)
        for receipt in receipts:
            groups[(int(receipt.adventurer_id), receipt.recovery_currency_code)].append(
                receipt
            )
        adventurer_ids = sorted({key[0] for key in groups})
        restored_items = []
        owner_ids = set()
        try:
            with db_manager.transaction():
                adventurers = (
                    AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids))
                    .order_by(AdventurerS1.id.asc())
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                adventurer_map = {int(row.id): row for row in adventurers}
                currencies = (
                    AdventurerS1Currency.query.filter(
                        AdventurerS1Currency.adventurer_id.in_(adventurer_ids),
                        AdventurerS1Currency.currency_code.in_(
                            [
                                "gold",
                                S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                            ]
                        ),
                    )
                    .order_by(
                        AdventurerS1Currency.adventurer_id.asc(),
                        AdventurerS1Currency.currency_code.asc(),
                    )
                    .populate_existing()
                    .with_for_update()
                    .all()
                )
                currency_map = {
                    (int(row.adventurer_id), row.currency_code): row
                    for row in currencies
                }
                frontier_rows = (
                    AdventurerS1CurrencyLog.query.with_entities(
                        AdventurerS1CurrencyLog.adventurer_id,
                        AdventurerS1CurrencyLog.currency_code,
                        func.max(AdventurerS1CurrencyLog.id),
                    )
                    .filter(
                        AdventurerS1CurrencyLog.adventurer_id.in_(adventurer_ids),
                        AdventurerS1CurrencyLog.currency_code.in_(
                            [
                                "gold",
                                S1CurrencyService.LOW_CONSUMPTION_CURRENCY_CODE,
                            ]
                        ),
                    )
                    .group_by(
                        AdventurerS1CurrencyLog.adventurer_id,
                        AdventurerS1CurrencyLog.currency_code,
                    )
                    .all()
                )
                frontier_map = {
                    (int(adventurer_id), currency_code): int(max_id or 0)
                    for adventurer_id, currency_code, max_id in frontier_rows
                }
                restore_logs = {}
                for group_key in sorted(groups):
                    adventurer_id, currency_code = group_key
                    group_receipts = groups[group_key]
                    adventurer = adventurer_map.get(adventurer_id)
                    currency_row = currency_map.get(group_key)
                    if adventurer is None or currency_row is None:
                        raise RuntimeError(
                            f"冒险者 #{adventurer_id} 的恢复余额行不存在"
                        )
                    before_values = {
                        cls._money(row.currency_balance_before)
                        for row in group_receipts
                    }
                    after_values = {
                        cls._money(row.currency_balance_after) for row in group_receipts
                    }
                    reclaim_log_ids = {
                        int(row.reclaim_currency_log_id) for row in group_receipts
                    }
                    if (
                        len(before_values) != 1
                        or len(after_values) != 1
                        or len(reclaim_log_ids) != 1
                    ):
                        raise RuntimeError(
                            f"冒险者 #{adventurer_id} 的逐笔凭证聚合状态不一致"
                        )
                    expected_after = next(iter(after_values))
                    expected_before = next(iter(before_values))
                    reclaim_log_id = next(iter(reclaim_log_ids))
                    if (
                        cls._money(currency_row.amount) != expected_after
                        or frontier_map.get(group_key, 0) != reclaim_log_id
                        or (
                            currency_code == "gold"
                            and cls._money(adventurer.gold) != expected_after
                        )
                    ):
                        raise RuntimeError(
                            f"冒险者 #{adventurer_id} 在回收后已有余额或流水变化，"
                            "禁止整批恢复",
                        )
                    amount = cls._money(
                        sum(row.reclaim_amount for row in group_receipts)
                    )
                    if cls._money(expected_after + amount) != expected_before:
                        raise RuntimeError(
                            f"冒险者 #{adventurer_id} 的恢复金额与冻结余额不守恒"
                        )
                    currency_row.amount = expected_before
                    if currency_code == "gold":
                        adventurer.gold = expected_before
                    log = S1CurrencyService._build_log(
                        adventurer_id,
                        currency_code,
                        amount,
                        expected_before,
                        cls.RESTORE_SOURCE_TYPE,
                        normalized_batch_id,
                        cls.RELATED_TYPE,
                        "条件恢复旧版低消费临时金重复增发回收",
                    )
                    restore_logs[group_key] = log
                    restored_items.append(
                        {
                            "adventurer_id": adventurer_id,
                            "adventurer_name": adventurer.name,
                            "currency_code": currency_code,
                            "amount": amount,
                            "balance_after": expected_before,
                        }
                    )
                    if adventurer.owner_id is not None:
                        owner_ids.add(int(adventurer.owner_id))
                ok, log_err = db_manager.add_all(
                    list(restore_logs.values()), flush=True
                )
                if not ok:
                    raise RuntimeError(log_err)
                restored_at = _portable_utc_now()
                for group_key, group_receipts in groups.items():
                    restore_log_id = int(restore_logs[group_key].id)
                    for receipt in group_receipts:
                        receipt.status = "restored"
                        receipt.restored_by = int(admin_user_id)
                        receipt.restored_at = restored_at
                        receipt.restore_currency_log_id = restore_log_id
                ok, flush_err = db_manager.flush()
                if not ok:
                    raise RuntimeError(flush_err)
        except Exception as exc:  # noqa: BLE001
            refreshed = AdventurerS1LowConsumptionOvercreditRepair.query.filter_by(
                batch_id=normalized_batch_id
            ).all()
            if refreshed and all(row.status == "restored" for row in refreshed):
                if any(row.restore_currency_log_id is None for row in refreshed):
                    return None, "该批次恢复凭证不完整，必须人工核查"
                return {
                    "batch_id": normalized_batch_id,
                    "restored_count": 0,
                    "restored_line_count": 0,
                    "restored_total": 0.0,
                    "already_restored": True,
                    "items": [],
                }, None
            return None, f"恢复旧临时金回收失败，已整批回滚：{exc}"

        cls._invalidate_activity(owner_ids)
        return {
            "batch_id": normalized_batch_id,
            "restored_count": len({item["adventurer_id"] for item in restored_items}),
            "restored_line_count": len(receipts),
            "restored_total": cls._money(
                sum(item["amount"] for item in restored_items)
            ),
            "already_restored": False,
            "items": restored_items,
        }, None

    @staticmethod
    def _invalidate_activity(owner_ids) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            invalidate_my_activity_cache,
        )

        for owner_id in owner_ids:
            invalidate_my_activity_cache(owner_id)
