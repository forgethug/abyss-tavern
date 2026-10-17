"""S1 金币/物品异常增长监控与历史回溯。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from flask import current_app
from sqlalchemy import String, and_, cast, exists, func, or_
from sqlalchemy.orm import Load

from db_repository import db_manager
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1CurrencyLog,
    AdventurerS1GainAnomaly,
    AdventurerS1ItemLog,
    AdventurerS1MarketListing,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from utils.time_utils import parse_iso_date, to_utc_iso

logger = logging.getLogger(__name__)


class S1GainAnomalyService:
    """从权威收益账本识别异常增长；仅供管理员审计，不执行处罚。"""

    SNAPSHOT_OPERATION = 'gain_anomaly_backfill'
    VALID_ASSET_KINDS = {'all', 'currency', 'item'}
    VALID_STATUSES = {'open', 'watching', 'resolved', 'dismissed'}
    VALID_SEVERITIES = {'medium', 'high'}
    EXCLUDED_CURRENCY_SOURCE_TYPES = frozenset({
        'commission_reward',
        'market_fee_refund',
        'secret_auction_refund',
    })
    EXCLUDED_ITEM_SOURCE_TYPES = frozenset({
        'commission_reward',
    })

    @staticmethod
    def _positive_int(key: str, default: int) -> int:
        try:
            return max(1, int(current_app.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _positive_float(key: str, default: float) -> float:
        try:
            return max(0.01, float(current_app.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    @classmethod
    def rules(cls) -> dict:
        raw_item_thresholds = current_app.config.get(
            'ADVENTURER_S1_GAIN_MONITOR_ITEM_QUANTITY_THRESHOLDS',
            {'common': 20, 'uncommon': 15, 'rare': 10, 'epic': 5, 'legendary': 2},
        )
        item_thresholds = {}
        for rarity in ('common', 'uncommon', 'rare', 'epic', 'legendary'):
            try:
                item_thresholds[rarity] = max(1, int((raw_item_thresholds or {}).get(rarity, 20)))
            except (TypeError, ValueError):
                item_thresholds[rarity] = 20
        return {
            'gold_single_threshold': cls._positive_float(
                'ADVENTURER_S1_GAIN_MONITOR_GOLD_SINGLE_THRESHOLD', 2000,
            ),
            'gold_hourly_threshold': cls._positive_float(
                'ADVENTURER_S1_GAIN_MONITOR_GOLD_HOURLY_THRESHOLD', 5000,
            ),
            'item_quantity_thresholds': item_thresholds,
            'item_hourly_multiplier': cls._positive_int(
                'ADVENTURER_S1_GAIN_MONITOR_ITEM_HOURLY_MULTIPLIER', 2,
            ),
            'recent_lookback_minutes': cls._positive_int(
                'ADVENTURER_S1_GAIN_MONITOR_RECENT_LOOKBACK_MINUTES', 120,
            ),
            'history_max_days': cls._positive_int(
                'ADVENTURER_S1_GAIN_MONITOR_HISTORY_MAX_DAYS', 365,
            ),
        }

    @staticmethod
    def _hour_bucket(value: datetime) -> datetime:
        return value.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _identity_payload(adv: AdventurerS1, user: Optional[User]) -> dict:
        return {
            'adventurer_id': int(adv.id),
            'adventurer_name': str(adv.name or f'冒险者#{adv.id}'),
            'owner_user_id': int(adv.owner_id) if adv.owner_id is not None else None,
            'owner_username': str(user.username) if user is not None else None,
            'owner_nickname': str(user.nickname or user.username) if user is not None else None,
        }

    @staticmethod
    def _source_breakdown(entries: list[dict], amount_key: str) -> list[dict]:
        grouped: dict[str, dict] = {}
        for entry in entries:
            source_type = str(entry.get('source_type') or 'unknown')
            row = grouped.setdefault(source_type, {'source_type': source_type, 'count': 0, 'amount': 0.0})
            row['count'] += 1
            row['amount'] = round(float(row['amount']) + float(entry.get(amount_key) or 0), 2)
        return sorted(grouped.values(), key=lambda row: (-float(row['amount']), row['source_type']))

    @staticmethod
    def _cancelled_market_listing_return_predicate(
        source_type_column: Any,
        related_type_column: Any,
        related_id_column: Any,
    ) -> Any:
        return and_(
            source_type_column == 'market',
            related_type_column == 'market_listing',
            exists().where(and_(
                cast(AdventurerS1MarketListing.id, String) == related_id_column,
                AdventurerS1MarketListing.status == 'cancelled',
            )),
        )

    @classmethod
    def _filter_excluded_candidates(cls, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        market_listing_ids: set[int] = set()
        for candidate in candidates:
            if (
                candidate.get('source_type') == 'market'
                and candidate.get('related_type') == 'market_listing'
            ):
                try:
                    market_listing_ids.add(int(candidate.get('related_id')))
                except (TypeError, ValueError):
                    continue
        cancelled_listing_ids = cls._cancelled_market_listing_ids(market_listing_ids)

        filtered = []
        for candidate in candidates:
            source_type = str(candidate.get('source_type') or 'unknown')
            asset_kind = str(candidate.get('asset_kind') or '')
            if asset_kind == 'currency' and source_type in cls.EXCLUDED_CURRENCY_SOURCE_TYPES:
                continue
            if asset_kind == 'item' and source_type in cls.EXCLUDED_ITEM_SOURCE_TYPES:
                continue
            if source_type == 'market' and candidate.get('related_type') == 'market_listing':
                try:
                    if int(candidate.get('related_id')) in cancelled_listing_ids:
                        continue
                except (TypeError, ValueError):
                    pass
            filtered.append(candidate)
        return filtered

    @staticmethod
    def _cancelled_market_listing_ids(listing_ids: Optional[set[int]] = None) -> set[int]:
        if listing_ids is not None and not listing_ids:
            return set()
        query = db_manager.query(AdventurerS1MarketListing.id).filter(
            AdventurerS1MarketListing.status == 'cancelled',
        )
        if listing_ids is not None:
            query = query.filter(AdventurerS1MarketListing.id.in_(listing_ids))
        return {
            int(row[0]) for row in query.all()
        }

    @classmethod
    def _visible_anomaly_query(cls) -> Any:
        return db_manager.query(AdventurerS1GainAnomaly).filter(
            ~and_(
                AdventurerS1GainAnomaly.asset_kind == 'currency',
                AdventurerS1GainAnomaly.source_type.in_(cls.EXCLUDED_CURRENCY_SOURCE_TYPES),
            ),
            ~and_(
                AdventurerS1GainAnomaly.asset_kind == 'item',
                AdventurerS1GainAnomaly.source_type.in_(cls.EXCLUDED_ITEM_SOURCE_TYPES),
            ),
            ~cls._cancelled_market_listing_return_predicate(
                AdventurerS1GainAnomaly.source_type,
                AdventurerS1GainAnomaly.related_type,
                AdventurerS1GainAnomaly.related_id,
            ),
        )

    @classmethod
    def _currency_candidates(cls, start_at: datetime, end_at: datetime, row_limit: int) -> tuple[list[dict], bool]:
        query = (
            db_manager.query(AdventurerS1CurrencyLog, AdventurerS1, User)
            .options(
                Load(AdventurerS1).load_only(
                    AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id,
                ).raiseload('*'),
                Load(User).load_only(User.id, User.username, User.nickname).raiseload('*'),
            )
            .join(AdventurerS1, AdventurerS1.id == AdventurerS1CurrencyLog.adventurer_id)
            .outerjoin(User, User.id == AdventurerS1.owner_id)
            .filter(
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.change_amount > 0,
                ~AdventurerS1CurrencyLog.source_type.in_(cls.EXCLUDED_CURRENCY_SOURCE_TYPES),
                AdventurerS1CurrencyLog.created_at >= start_at,
                AdventurerS1CurrencyLog.created_at < end_at,
            )
            .order_by(AdventurerS1CurrencyLog.created_at.asc(), AdventurerS1CurrencyLog.id.asc())
            .limit(row_limit + 1)
        )
        rules = cls.rules()
        single_threshold = float(rules['gold_single_threshold'])
        hourly_threshold = float(rules['gold_hourly_threshold'])
        candidates: list[dict] = []
        hourly: dict[int, list[dict]] = defaultdict(list)
        active_bucket: Optional[datetime] = None

        def append_hour_candidates(bucket: Optional[datetime]) -> None:
            if bucket is None:
                return
            for adventurer_id, entries in hourly.items():
                total = round(sum(float(entry['gain_amount']) for entry in entries), 2)
                if len(entries) < 2 or total < hourly_threshold:
                    continue
                if max(float(entry['gain_amount']) for entry in entries) >= single_threshold:
                    continue
                latest = entries[-1]
                source_types = {str(entry['source_type']) for entry in entries}
                candidates.append({
                    **latest,
                    'event_key': f'currency-hour:{adventurer_id}:{bucket.isoformat()}',
                    'asset_kind': 'currency',
                    'detector_rule': 'gold_hourly_gain',
                    'severity': 'high' if total >= hourly_threshold * 2 else 'medium',
                    'gain_amount': total,
                    'source_log_id': int(latest['source_log_id']),
                    'source_type': next(iter(source_types)) if len(source_types) == 1 else 'multiple_sources',
                    'description': f'同一小时内 {len(entries)} 笔金币收入累计 {total:g}',
                    'occurred_at': to_utc_iso(bucket),
                    'evidence': {
                        'threshold': hourly_threshold,
                        'window_started_at': to_utc_iso(bucket),
                        'window_ended_at': to_utc_iso(bucket + timedelta(hours=1)),
                        'event_count': len(entries),
                        'currency_log_ids': [int(entry['source_log_id']) for entry in entries[:100]],
                        'log_ids_truncated': len(entries) > 100,
                        'source_breakdown': cls._source_breakdown(entries, 'gain_amount'),
                    },
                })

        truncated = False
        row_count = 0

        for log, adv, user in query.yield_per(2000):
            row_count += 1
            if row_count > row_limit:
                truncated = True
                break
            bucket = cls._hour_bucket(log.created_at)
            if active_bucket is not None and bucket != active_bucket:
                append_hour_candidates(active_bucket)
                hourly.clear()
            active_bucket = bucket
            amount = round(float(log.change_amount or 0), 2)
            identity = cls._identity_payload(adv, user)
            base = {
                **identity,
                'source_log_id': int(log.id),
                'currency_code': 'gold',
                'item_name': None,
                'item_type': None,
                'item_rarity': None,
                'gain_amount': amount,
                'balance_after': round(float(log.balance_after or 0), 2),
                'source_type': str(log.source_type or 'unknown'),
                'related_id': str(log.related_id) if log.related_id is not None else None,
                'related_type': str(log.related_type) if log.related_type else None,
                'description': str(log.description) if log.description else None,
                'occurred_at': to_utc_iso(log.created_at),
            }
            if amount >= single_threshold:
                candidates.append({
                    **base,
                    'event_key': f'currency-single:{log.id}',
                    'asset_kind': 'currency',
                    'detector_rule': 'gold_single_gain',
                    'severity': 'high' if amount >= single_threshold * 2 else 'medium',
                    'evidence': {'threshold': single_threshold, 'currency_log_ids': [int(log.id)]},
                })
            hourly[int(adv.id)].append(base)
        append_hour_candidates(active_bucket)
        return candidates, truncated

    @classmethod
    def _item_candidates(cls, start_at: datetime, end_at: datetime, row_limit: int) -> tuple[list[dict], bool]:
        cancelled_listing_ids = cls._cancelled_market_listing_ids()
        query = (
            db_manager.query(AdventurerS1ItemLog, AdventurerS1, User)
            .options(
                Load(AdventurerS1).load_only(
                    AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id,
                ).raiseload('*'),
                Load(User).load_only(User.id, User.username, User.nickname).raiseload('*'),
            )
            .join(AdventurerS1, AdventurerS1.id == AdventurerS1ItemLog.adventurer_id)
            .outerjoin(User, User.id == AdventurerS1.owner_id)
            .filter(
                AdventurerS1ItemLog.change_type.in_(('gain', 'overflow')),
                AdventurerS1ItemLog.quantity > 0,
                ~AdventurerS1ItemLog.source_type.in_(cls.EXCLUDED_ITEM_SOURCE_TYPES),
                AdventurerS1ItemLog.created_at >= start_at,
                AdventurerS1ItemLog.created_at < end_at,
            )
            .order_by(AdventurerS1ItemLog.created_at.asc(), AdventurerS1ItemLog.id.asc())
            .limit(row_limit + len(cancelled_listing_ids) + 1)
        )
        rules = cls.rules()
        thresholds: dict[str, int] = rules['item_quantity_thresholds']
        multiplier = int(rules['item_hourly_multiplier'])
        candidates: list[dict] = []
        hourly: dict[tuple[int, str], list[dict]] = defaultdict(list)
        active_bucket: Optional[datetime] = None

        def append_hour_candidates(bucket: Optional[datetime]) -> None:
            if bucket is None:
                return
            for (adventurer_id, item_name), entries in hourly.items():
                rarity = str(entries[-1]['item_rarity'] or 'common')
                single_threshold = int(thresholds.get(rarity, thresholds['common']))
                hourly_threshold = single_threshold * multiplier
                total = int(sum(int(entry['gain_amount']) for entry in entries))
                if len(entries) < 2 or total < hourly_threshold:
                    continue
                if max(int(entry['gain_amount']) for entry in entries) >= single_threshold:
                    continue
                latest = entries[-1]
                digest = hashlib.sha256(item_name.encode('utf-8')).hexdigest()[:16]
                source_types = {str(entry['source_type']) for entry in entries}
                candidates.append({
                    **latest,
                    'event_key': f'item-hour:{adventurer_id}:{digest}:{bucket.isoformat()}',
                    'asset_kind': 'item',
                    'detector_rule': 'item_hourly_gain',
                    'severity': 'high' if total >= hourly_threshold * 2 else 'medium',
                    'gain_amount': total,
                    'source_log_id': int(latest['source_log_id']),
                    'source_type': next(iter(source_types)) if len(source_types) == 1 else 'multiple_sources',
                    'description': f'同一小时内 {len(entries)} 笔获得「{item_name}」累计 {total} 件',
                    'occurred_at': to_utc_iso(bucket),
                    'evidence': {
                        'threshold': hourly_threshold,
                        'window_started_at': to_utc_iso(bucket),
                        'window_ended_at': to_utc_iso(bucket + timedelta(hours=1)),
                        'event_count': len(entries),
                        'item_log_ids': [int(entry['source_log_id']) for entry in entries[:100]],
                        'log_ids_truncated': len(entries) > 100,
                        'source_breakdown': cls._source_breakdown(entries, 'gain_amount'),
                    },
                })

        truncated = False
        row_count = 0

        for log, adv, user in query.yield_per(2000):
            if log.source_type == 'market' and log.related_type == 'market_listing':
                try:
                    if int(log.related_id) in cancelled_listing_ids:
                        continue
                except (TypeError, ValueError):
                    pass
            row_count += 1
            if row_count > row_limit:
                truncated = True
                break
            bucket = cls._hour_bucket(log.created_at)
            if active_bucket is not None and bucket != active_bucket:
                append_hour_candidates(active_bucket)
                hourly.clear()
            active_bucket = bucket
            rarity = str(log.item_rarity or 'common').strip().lower()
            threshold = int(thresholds.get(rarity, thresholds['common']))
            quantity = int(log.quantity or 0)
            identity = cls._identity_payload(adv, user)
            base = {
                **identity,
                'source_log_id': int(log.id),
                'currency_code': None,
                'item_name': str(log.item_name or ''),
                'item_type': str(log.item_type or ''),
                'item_rarity': rarity,
                'gain_amount': quantity,
                'balance_after': None,
                'source_type': str(log.source_type or 'unknown'),
                'related_id': str(log.related_id) if log.related_id is not None else None,
                'related_type': str(log.related_type) if log.related_type else None,
                'description': str(log.description) if log.description else None,
                'occurred_at': to_utc_iso(log.created_at),
            }
            if quantity >= threshold:
                candidates.append({
                    **base,
                    'event_key': f'item-single:{log.id}',
                    'asset_kind': 'item',
                    'detector_rule': 'item_single_gain',
                    'severity': 'high' if rarity == 'legendary' or quantity >= threshold * 2 else 'medium',
                    'evidence': {'threshold': threshold, 'item_log_ids': [int(log.id)]},
                })
            hourly[(int(adv.id), str(log.item_name or ''))].append(base)
        append_hour_candidates(active_bucket)
        return candidates, truncated

    @classmethod
    def _filter_existing(cls, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        keys = [str(candidate['event_key']) for candidate in candidates]
        existing = set()
        for offset in range(0, len(keys), 5000):
            batch = keys[offset:offset + 5000]
            existing.update(
                row[0] for row in (
                    db_manager.query(AdventurerS1GainAnomaly.event_key)
                    .filter(AdventurerS1GainAnomaly.event_key.in_(batch))
                    .all()
                )
            )
        return [candidate for candidate in candidates if candidate['event_key'] not in existing]

    @classmethod
    def collect_candidates(
        cls,
        start_at: datetime,
        end_at: datetime,
        asset_kind: str = 'all',
    ) -> tuple[dict, Optional[str]]:
        if asset_kind not in cls.VALID_ASSET_KINDS:
            return {}, '收益类型无效'
        row_limit = cls._positive_int('ADVENTURER_S1_GAIN_MONITOR_HISTORY_MAX_LOG_ROWS', 300000)
        candidates: list[dict] = []
        truncated = False
        try:
            if asset_kind in ('all', 'currency'):
                rows, was_truncated = cls._currency_candidates(start_at, end_at, row_limit)
                candidates.extend(rows)
                truncated = truncated or was_truncated
            if asset_kind in ('all', 'item'):
                rows, was_truncated = cls._item_candidates(start_at, end_at, row_limit)
                candidates.extend(rows)
                truncated = truncated or was_truncated
            candidates_by_key = {str(row['event_key']): row for row in candidates}
            candidates = cls._filter_excluded_candidates(list(candidates_by_key.values()))
            candidates = cls._filter_existing(candidates)
            candidates.sort(key=lambda row: str(row.get('occurred_at') or ''), reverse=True)
            max_candidates = cls._positive_int(
                'ADVENTURER_S1_GAIN_MONITOR_HISTORY_MAX_CANDIDATES', 5000,
            )
            candidate_truncated = len(candidates) > max_candidates
            candidates = candidates[:max_candidates]
            # 历史扫描完成后先结束只读事务，再写 Redis 快照或进入后续短写事务。
            ok, commit_err = db_manager.commit()
            if not ok:
                return {}, commit_err
            return {
                'candidates': candidates,
                'candidate_count': len(candidates),
                'scan_truncated': truncated,
                'candidate_truncated': candidate_truncated,
                'start_at': to_utc_iso(start_at),
                'end_at': to_utc_iso(end_at),
                'asset_kind': asset_kind,
                'rules': cls.rules(),
            }, None
        except Exception as exc:
            db_manager.rollback()
            logger.exception('S1 收益异常扫描失败')
            return {}, str(exc)

    @classmethod
    def _record_candidates(cls, candidates: list[dict], *, is_historical: bool) -> tuple[dict, Optional[str]]:
        pending = cls._filter_excluded_candidates(list(candidates))
        last_err: Optional[str] = None
        for _attempt in range(3):
            pending = cls._filter_existing(pending)
            if not pending:
                return {'created_count': 0}, None
            now = _portable_utc_now()
            rows = []
            for candidate in pending:
                occurred_at = parse_iso_date(str(candidate.get('occurred_at') or '')) or now
                rows.append(AdventurerS1GainAnomaly(
                    event_key=str(candidate['event_key']),
                    asset_kind=str(candidate['asset_kind']),
                    detector_rule=str(candidate['detector_rule']),
                    severity=str(candidate['severity']),
                    status='watching' if candidate.get('severity') == 'high' else 'open',
                    is_historical=bool(is_historical),
                    adventurer_id=int(candidate['adventurer_id']),
                    adventurer_name=str(candidate['adventurer_name']),
                    owner_user_id=candidate.get('owner_user_id'),
                    owner_username=candidate.get('owner_username'),
                    owner_nickname=candidate.get('owner_nickname'),
                    source_log_id=candidate.get('source_log_id'),
                    currency_code=candidate.get('currency_code'),
                    item_name=candidate.get('item_name'),
                    item_type=candidate.get('item_type'),
                    item_rarity=candidate.get('item_rarity'),
                    gain_amount=float(candidate.get('gain_amount') or 0),
                    balance_after=candidate.get('balance_after'),
                    source_type=str(candidate.get('source_type') or 'unknown'),
                    related_id=candidate.get('related_id'),
                    related_type=candidate.get('related_type'),
                    description=candidate.get('description'),
                    evidence=candidate.get('evidence') or {},
                    occurred_at=occurred_at,
                    detected_at=now,
                ))
            ok, add_err = db_manager.add_all(rows, flush=False)
            if not ok:
                db_manager.rollback()
                last_err = add_err
                continue
            ok, commit_err = db_manager.commit()
            if ok:
                return {'created_count': len(rows)}, None
            # 并发扫描可能同时插入同一 event_key；回滚后重查缺失项再重试。
            last_err = commit_err
        return {}, last_err or '收益异常监控记录写入失败'

    @classmethod
    def monitor_recent(cls) -> tuple[dict, Optional[str]]:
        end_at = _portable_utc_now()
        start_at = end_at - timedelta(minutes=int(cls.rules()['recent_lookback_minutes']))
        preview, err = cls.collect_candidates(start_at, end_at, 'all')
        if err:
            return {}, err
        result, err = cls._record_candidates(preview['candidates'], is_historical=False)
        if err:
            return {}, err
        return {**result, 'scanned_from': preview['start_at'], 'scanned_to': preview['end_at']}, None

    @classmethod
    def preview_history(
        cls, admin_user_id: int, lookback_days: int, asset_kind: str,
    ) -> tuple[dict, Optional[str]]:
        max_days = int(cls.rules()['history_max_days'])
        try:
            days = int(lookback_days)
        except (TypeError, ValueError):
            return {}, '回溯天数必须为整数'
        if days < 1 or days > max_days:
            return {}, f'回溯天数必须在 1～{max_days} 之间'
        end_at = _portable_utc_now()
        preview, err = cls.collect_candidates(end_at - timedelta(days=days), end_at, asset_kind)
        if err:
            return {}, err
        token = S1AdminScanSnapshotService.store(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview,
        )
        result = {key: value for key, value in preview.items() if key != 'candidates'}
        result['items'] = preview['candidates'][:100]
        result['sample_truncated'] = len(preview['candidates']) > 100
        result['snapshot_token'] = token
        if token is None:
            result['snapshot_error'] = '预览快照暂存失败，请确认共享 Redis 可用后重试'
        return result, None

    @classmethod
    def execute_history(cls, admin_user_id: int, preview_token: object) -> tuple[dict, Optional[str]]:
        preview = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
        )
        if preview is None:
            return {}, '预览快照不存在或已过期，请重新预览'
        candidates = preview.get('candidates')
        if not isinstance(candidates, list):
            return {}, '预览快照格式无效，请重新预览'
        result, err = cls._record_candidates(candidates, is_historical=True)
        if err:
            return {}, err
        S1AdminScanSnapshotService.delete(cls.SNAPSHOT_OPERATION, admin_user_id, preview_token)
        return {
            **result,
            'preview_candidate_count': len(candidates),
            'skipped_existing_count': max(0, len(candidates) - int(result['created_count'])),
        }, None

    @classmethod
    def list_records(
        cls,
        page: int,
        per_page: int,
        *,
        status: Optional[str] = None,
        asset_kind: Optional[str] = None,
        severity: Optional[str] = None,
        search: str = '',
    ) -> tuple[dict, Optional[str]]:
        if status and status != 'active' and status not in cls.VALID_STATUSES:
            return {}, '处理状态无效'
        if asset_kind and asset_kind not in ('currency', 'item'):
            return {}, '收益类型无效'
        if severity and severity not in cls.VALID_SEVERITIES:
            return {}, '严重级别无效'
        query = cls._visible_anomaly_query()
        if status == 'active':
            query = query.filter(AdventurerS1GainAnomaly.status.in_(('open', 'watching')))
        elif status:
            query = query.filter(AdventurerS1GainAnomaly.status == status)
        if asset_kind:
            query = query.filter(AdventurerS1GainAnomaly.asset_kind == asset_kind)
        if severity:
            query = query.filter(AdventurerS1GainAnomaly.severity == severity)
        search_text = str(search or '').strip()
        if search_text:
            pattern = f'%{search_text}%'
            query = query.filter(or_(
                AdventurerS1GainAnomaly.adventurer_name.ilike(pattern),
                AdventurerS1GainAnomaly.owner_username.ilike(pattern),
                AdventurerS1GainAnomaly.owner_nickname.ilike(pattern),
                AdventurerS1GainAnomaly.item_name.ilike(pattern),
                AdventurerS1GainAnomaly.source_type.ilike(pattern),
                AdventurerS1GainAnomaly.description.ilike(pattern),
            ))
        try:
            total = query.count()
            rows = (
                query.order_by(
                    AdventurerS1GainAnomaly.detected_at.desc(),
                    AdventurerS1GainAnomaly.id.desc(),
                )
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all()
            )
            status_counts = dict(
                cls._visible_anomaly_query()
                .with_entities(
                    AdventurerS1GainAnomaly.status,
                    func.count(AdventurerS1GainAnomaly.id),
                )
                .group_by(AdventurerS1GainAnomaly.status)
                .all()
            )
            return {
                'items': [row.to_dict() for row in rows],
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': (total + per_page - 1) // per_page,
                'summary': {
                    'status_counts': {key: int(value) for key, value in status_counts.items()},
                    'rules': cls.rules(),
                },
            }, None
        except Exception as exc:
            db_manager.rollback()
            return {}, str(exc)

    @classmethod
    def update_status(
        cls, anomaly_id: int, admin_user_id: int, status: str, note: str = '',
    ) -> tuple[dict, Optional[str]]:
        if status not in cls.VALID_STATUSES:
            return {}, '处理状态无效'
        clean_note = str(note or '').strip()
        if len(clean_note) > 500:
            return {}, '处理备注不能超过 500 字'
        try:
            row = (
                db_manager.query(AdventurerS1GainAnomaly)
                .filter(AdventurerS1GainAnomaly.id == anomaly_id)
                .with_for_update()
                .first()
            )
            if row is None:
                return {}, '监控记录不存在'
            row.status = status
            row.resolution_note = clean_note or None
            if status in ('resolved', 'dismissed'):
                row.resolved_by_user_id = int(admin_user_id)
                row.resolved_at = _portable_utc_now()
            else:
                row.resolved_by_user_id = None
                row.resolved_at = None
            ok, commit_err = db_manager.commit()
            if not ok:
                return {}, commit_err
            return row.to_dict(), None
        except Exception as exc:
            db_manager.rollback()
            return {}, str(exc)


def monitor_recent_s1_gains_job() -> None:
    """Scheduler 入口：短窗口扫描并显式提交监控记录。"""
    result, err = S1GainAnomalyService.monitor_recent()
    if err:
        logger.error('S1 收益异常监控任务失败: %s', err)
        return
    if int(result.get('created_count') or 0) > 0:
        logger.warning('S1 收益异常监控新增 %s 条记录', result['created_count'])
