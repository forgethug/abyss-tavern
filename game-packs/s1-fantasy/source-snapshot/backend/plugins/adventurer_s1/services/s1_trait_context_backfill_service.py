"""S1 非归档特质上下文路由的有界后台补齐。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

from flask import current_app
from sqlalchemy import case, func, select

from db_repository import db_manager
from db_repository.s1_trait_context_backfill_repository import (
    read_backfill_cache_rows,
    read_backfill_rows,
)
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Trait,
)
from plugins.adventurer_s1.services.s1_description_ai_service import (
    description_source_hash,
    enqueue_description_compression_items,
    get_short_trait_max_chars,
)
from plugins.adventurer_s1.services.s1_realm_service import S1RealmService
from plugins.adventurer_s1.services.s1_trait_context_selection_service import (
    TRAIT_CONTEXT_REVIEW_PROTOCOL_VERSION,
    project_completed_trait_context_review_receipt,
    project_trait_context_rule,
)

logger = logging.getLogger(__name__)


@dataclass
class _CoverageCycle:
    cursor: int = 0
    scanned: int = 0
    passed: int = 0
    approved: int = 0
    corrected: int = 0
    unclassifiable: int = 0
    legacy_passed: int = 0
    rejected: int = 0
    inflight: int = 0
    uncovered: int = 0


_STATE_LOCK = Lock()
# 仅用于减少重复扫描和汇总覆盖率日志；缓存行的哈希、状态与 claim 才是
# 跨 worker 正确性权威。进程重启或多 scheduler 进程各自重扫只会带来冗余查询。
_COVERAGE_BY_REALM: dict[str, _CoverageCycle] = {}


def _config_int(key: str, default: int) -> int:
    try:
        return max(1, int(current_app.config.get(key, default) or default))
    except (TypeError, ValueError):
        return max(1, int(default))


def _trait_counts_subquery(realm_code: str):
    return (
        select(
            AdventurerS1Trait.adventurer_id.label('adventurer_id'),
            func.count(AdventurerS1Trait.id).label('trait_count'),
        )
        .join(
            AdventurerS1,
            AdventurerS1.id == AdventurerS1Trait.adventurer_id,
        )
        .filter(
            AdventurerS1.realm_code == realm_code,
            AdventurerS1Trait.is_archived.is_(False),
        )
        .group_by(AdventurerS1Trait.adventurer_id)
        .subquery()
    )


def _priority_trait_rows(
    realm_code: str,
    *,
    recent_since: datetime,
    limit: int,
):
    trait_counts = _trait_counts_subquery(realm_code)
    priority_group = case(
        (AdventurerS1.status.in_(('in_party', 'exploring')), 0),
        (AdventurerS1.last_owner_action_at >= recent_since, 1),
        else_=2,
    )
    statement = (
        select(
            AdventurerS1Trait.id,
            AdventurerS1Trait.name,
            AdventurerS1Trait.description,
            AdventurerS1Trait.adventurer_id,
        )
        .join(
            AdventurerS1,
            AdventurerS1.id == AdventurerS1Trait.adventurer_id,
        )
        .join(
            trait_counts,
            trait_counts.c.adventurer_id == AdventurerS1Trait.adventurer_id,
        )
        .filter(
            AdventurerS1.realm_code == realm_code,
            AdventurerS1Trait.is_archived.is_(False),
            AdventurerS1Trait.description != '',
        )
        .order_by(
            priority_group.asc(),
            trait_counts.c.trait_count.desc(),
            AdventurerS1.last_owner_action_at.desc().nullslast(),
            AdventurerS1Trait.updated_at.desc(),
            AdventurerS1Trait.id.asc(),
        )
        .limit(limit)
    )
    return read_backfill_rows(statement)


def _cursor_trait_rows(realm_code: str, *, cursor: int, limit: int):
    statement = (
        select(
            AdventurerS1Trait.id,
            AdventurerS1Trait.name,
            AdventurerS1Trait.description,
            AdventurerS1Trait.adventurer_id,
        )
        .join(
            AdventurerS1,
            AdventurerS1.id == AdventurerS1Trait.adventurer_id,
        )
        .filter(
            AdventurerS1.realm_code == realm_code,
            AdventurerS1Trait.is_archived.is_(False),
            AdventurerS1Trait.description != '',
            AdventurerS1Trait.id > cursor,
        )
        .order_by(AdventurerS1Trait.id.asc())
        .limit(limit)
    )
    return read_backfill_rows(statement)


def _trait_item(row) -> dict:
    description = str(row.description or '').strip()
    return {
        'source_hash': description_source_hash(description, kind='trait'),
        'kind': 'trait',
        'name': str(row.name or '').strip(),
        'description': description,
    }


def _route_coverage_state(
    item: dict,
    cache_row: object | None,
) -> str:
    if cache_row is None:
        return 'uncovered'
    projected_rule, errors = project_trait_context_rule(
        getattr(cache_row, 'trait_context_rule', None),
    )
    exact_rule = bool(
        projected_rule is not None
        and not errors
        and projected_rule.classification_source_hash == item['source_hash']
    )
    audit_status = str(
        getattr(cache_row, 'trait_context_audit_status', '') or ''
    )
    if exact_rule and audit_status == 'passed':
        receipt = getattr(cache_row, 'trait_context_audit_receipt', None)
        if receipt is not None:
            if (
                not isinstance(receipt, dict)
                or receipt.get('protocol_version')
                != TRAIT_CONTEXT_REVIEW_PROTOCOL_VERSION
            ):
                return 'uncovered'
            decision, receipt_errors = (
                project_completed_trait_context_review_receipt(
                    receipt,
                    current_rule=getattr(
                        cache_row,
                        'trait_context_rule',
                        None,
                    ),
                    classification_source_hash=item['source_hash'],
                )
            )
            if decision is not None and not receipt_errors:
                return decision
            return 'uncovered'
        return 'legacy_passed'
    receipt = getattr(cache_row, 'trait_context_audit_receipt', None)
    if (
        isinstance(receipt, dict)
        and receipt.get('protocol_version')
        == TRAIT_CONTEXT_REVIEW_PROTOCOL_VERSION
        and receipt.get('state') in {'pending', 'processing', 'retryable'}
    ):
        return 'inflight'
    if exact_rule and audit_status == 'rejected':
        return 'rejected'
    if str(getattr(cache_row, 'status', '') or '') in {
        'pending',
        'processing',
        'retryable',
    }:
        return 'inflight'
    return 'uncovered'


def _coverage_counts(items: list[dict], cache_rows: dict[str, object]) -> dict[str, int]:
    counts = {
        'passed': 0,
        'approved': 0,
        'corrected': 0,
        'unclassifiable': 0,
        'legacy_passed': 0,
        'rejected': 0,
        'inflight': 0,
        'uncovered': 0,
    }
    for item in items:
        state = _route_coverage_state(
            item,
            cache_rows.get(item['source_hash']),
        )
        if state in {
            'approved', 'corrected', 'unclassifiable', 'legacy_passed',
        }:
            counts['passed'] += 1
            counts[state] += 1
        else:
            counts[state] += 1
    return counts


def _finish_cycle(realm_code: str) -> dict[str, int] | None:
    with _STATE_LOCK:
        state = _COVERAGE_BY_REALM.setdefault(realm_code, _CoverageCycle())
        if state.scanned <= 0:
            state.cursor = 0
            return None
        snapshot = {
            'scanned': state.scanned,
            'passed': state.passed,
            'approved': state.approved,
            'corrected': state.corrected,
            'unclassifiable': state.unclassifiable,
            'legacy_passed': state.legacy_passed,
            'rejected': state.rejected,
            'inflight': state.inflight,
            'uncovered': state.uncovered,
        }
        _COVERAGE_BY_REALM[realm_code] = _CoverageCycle()
        return snapshot


def _record_cursor_page(
    realm_code: str,
    *,
    last_trait_id: int,
    counts: dict[str, int],
    page_size: int,
    cycle_complete: bool,
) -> dict[str, int] | None:
    with _STATE_LOCK:
        state = _COVERAGE_BY_REALM.setdefault(realm_code, _CoverageCycle())
        state.cursor = 0 if cycle_complete else last_trait_id
        state.scanned += page_size
        state.passed += counts['passed']
        state.approved += counts['approved']
        state.corrected += counts['corrected']
        state.unclassifiable += counts['unclassifiable']
        state.legacy_passed += counts['legacy_passed']
        state.rejected += counts['rejected']
        state.inflight += counts['inflight']
        state.uncovered += counts['uncovered']
        if not cycle_complete:
            return None
        snapshot = {
            'scanned': state.scanned,
            'passed': state.passed,
            'approved': state.approved,
            'corrected': state.corrected,
            'unclassifiable': state.unclassifiable,
            'legacy_passed': state.legacy_passed,
            'rejected': state.rejected,
            'inflight': state.inflight,
            'uncovered': state.uncovered,
        }
        _COVERAGE_BY_REALM[realm_code] = _CoverageCycle()
        return snapshot


def _log_cycle_coverage(realm_code: str, snapshot: dict[str, int] | None) -> None:
    if not snapshot:
        return
    scanned = max(1, int(snapshot['scanned']))
    logger.info(
        'S1_TRAIT_CONTEXT_BACKFILL_COVERAGE '
        'realm=%s scanned=%s passed=%s approved=%s corrected=%s '
        'unclassifiable=%s legacy_passed=%s rejected=%s inflight=%s uncovered=%s '
        'passed_pct=%.2f',
        realm_code,
        snapshot['scanned'],
        snapshot['passed'],
        snapshot['approved'],
        snapshot['corrected'],
        snapshot['unclassifiable'],
        snapshot['legacy_passed'],
        snapshot['rejected'],
        snapshot['inflight'],
        snapshot['uncovered'],
        snapshot['passed'] * 100.0 / scanned,
    )


def submit_trait_context_backfill() -> int:
    """有界扫描当前 realm 的非归档特质并异步补齐，不等待任何 AI。"""
    if not current_app.config.get(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_ENABLED',
        True,
    ):
        return 0

    realm_code = S1RealmService.current_runtime_realm()
    priority_limit = _config_int(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_PRIORITY_SCAN_SIZE',
        300,
    )
    cursor_limit = _config_int(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_CURSOR_SCAN_SIZE',
        400,
    )
    max_batches = _config_int(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_MAX_BATCHES_PER_RUN',
        2,
    )
    max_items = _config_int(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_MAX_ITEMS_PER_RUN',
        40,
    )
    recent_days = _config_int(
        'ADVENTURER_S1_TRAIT_CONTEXT_BACKFILL_RECENT_DAYS',
        30,
    )

    from plugins.adventurer_s1.services.s1_description_backlog_service import submit_description_backlog
    # 复用同一 scheduler 和总批次预算，先给实际携带资产一个批次；
    # 默认两批时，另一批仍用于原有全服特质补扫。
    backlog_queued = submit_description_backlog(
        realm_code=realm_code, max_items=max_items, max_batches=1,
    )
    if backlog_queued:
        max_batches -= 1
        max_items -= backlog_queued
        if max_batches <= 0 or max_items <= 0:
            return backlog_queued

    with _STATE_LOCK:
        cursor = _COVERAGE_BY_REALM.setdefault(
            realm_code,
            _CoverageCycle(),
        ).cursor

    try:
        priority_rows = _priority_trait_rows(
            realm_code,
            recent_since=_portable_utc_now() - timedelta(days=recent_days),
            limit=priority_limit,
        )
        cursor_rows = _cursor_trait_rows(
            realm_code,
            cursor=cursor,
            limit=cursor_limit,
        )
        if not cursor_rows and cursor > 0:
            _log_cycle_coverage(realm_code, _finish_cycle(realm_code))
            cursor = 0
            cursor_rows = _cursor_trait_rows(
                realm_code,
                cursor=0,
                limit=cursor_limit,
            )

        priority_items = [
            item
            for item in (_trait_item(row) for row in priority_rows)
            if item['description']
        ]
        cursor_items = [
            item
            for item in (_trait_item(row) for row in cursor_rows)
            if item['description']
        ]
        unique_items: list[dict] = []
        seen_hashes: set[str] = set()
        for item in priority_items + cursor_items:
            if not item['description'] or item['source_hash'] in seen_hashes:
                continue
            seen_hashes.add(item['source_hash'])
            unique_items.append(item)

        cache_rows = read_backfill_cache_rows(seen_hashes)
        priority_counts = _coverage_counts(priority_items, cache_rows)
        cursor_counts = _coverage_counts(cursor_items, cache_rows)
        cycle_complete = len(cursor_rows) < cursor_limit
        coverage_snapshot = None
        if cursor_rows:
            coverage_snapshot = _record_cursor_page(
                realm_code,
                last_trait_id=int(cursor_rows[-1].id),
                counts=cursor_counts,
                page_size=len(cursor_rows),
                cycle_complete=cycle_complete,
            )
        db_manager.remove()

        queued = enqueue_description_compression_items(
            unique_items,
            max_batches=max_batches,
            max_items=max_items,
        )
        short_trait_max_chars = get_short_trait_max_chars()
        short_trait_sources = sum(
            1 for item in unique_items
            if len(item['description']) <= short_trait_max_chars
        )
        logger.info(
            'S1_TRAIT_CONTEXT_BACKFILL_SCAN '
            'realm=%s priority_scanned=%s cursor_scanned=%s unique_sources=%s '
            'priority_passed=%s priority_rejected=%s priority_inflight=%s '
            'priority_uncovered=%s short_trait_sources=%s long_trait_sources=%s '
            'queued=%s cursor=%s cycle_complete=%s',
            realm_code,
            len(priority_rows),
            len(cursor_rows),
            len(unique_items),
            priority_counts['passed'],
            priority_counts['rejected'],
            priority_counts['inflight'],
            priority_counts['uncovered'],
            short_trait_sources,
            len(unique_items) - short_trait_sources,
            queued,
            int(cursor_rows[-1].id) if cursor_rows else 0,
            cycle_complete,
        )
        _log_cycle_coverage(realm_code, coverage_snapshot)
        return backlog_queued + queued
    except Exception:
        db_manager.rollback()
        logger.exception(
            'S1_TRAIT_CONTEXT_BACKFILL_FAILED realm=%s cursor=%s',
            realm_code,
            cursor,
        )
        return 0
    finally:
        db_manager.remove()


def _reset_trait_context_backfill_state_for_tests() -> None:
    with _STATE_LOCK:
        _COVERAGE_BY_REALM.clear()
