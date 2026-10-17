"""S1 势力档案：稀疏维护、版本审计与按世界线有界再注入。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
import logging
import uuid
from datetime import datetime

from flask import current_app
from sqlalchemy import or_, text

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1FactionProfile,
    AdventurerS1FactionProfileHistory,
    AdventurerS1FactionProfileThread,
    AdventurerS1WorldStoryThread,
)
from plugins.adventurer_s1.services.s1_world_outline_contract import (
    FACTION_PROFILE_FACETS,
    FACTION_PROFILE_STATUSES,
    FACTION_PROFILE_THREAD_ROLES,
    FACTION_PROFILE_VISIBILITIES,
    MAX_FACTION_PROFILE_DB_INTEGER,
    MAX_FACTION_PROFILE_UPDATE_ITEMS,
    normalize_faction_profile_updates,
)


logger = logging.getLogger(__name__)


_FACET_LABELS = {
    'scale': '规模',
    'territory': '所在地与据点',
    'operating_range': '活动范围',
    'history': '沿革',
    'governance': '治理偏好',
    'internal_structure': '内部结构',
    'capabilities': '技术与能力',
    'knowledge': '知识边界',
    'resources': '资源基础',
    'culture': '文化与习惯',
    'current_agenda': '当前议程',
    'current_state': '当前处境',
}
_FACET_PROMPT_ORDER = (
    'current_state', 'capabilities', 'resources', 'current_agenda',
    'operating_range', 'territory', 'scale', 'knowledge',
    'internal_structure', 'governance', 'history', 'culture',
)
_FACTION_PROFILE_CAPACITY_LOCK_KEY = 731_050_141
_BOOTSTRAP_STATUS_PRIORITY = {
    'escalated': 0,
    'active': 1,
    'closing': 2,
    'seeded': 3,
    'dormant': 4,
    'resolved': 5,
}


class _NoFactionProfileChange(Exception):
    """Internal savepoint signal for a valid proposal with no state change."""


def _enabled() -> bool:
    return bool(current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_ENABLED', True))


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive_db_integer(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 < value <= MAX_FACTION_PROFILE_DB_INTEGER else 0


def _lock_creation_capacity() -> None:
    """Serialize the global profile-count boundary on production PostgreSQL."""
    session = db_manager.query(AdventurerS1FactionProfile).session
    if session.get_bind().dialect.name != 'postgresql':
        return
    session.execute(
        text('SELECT pg_advisory_xact_lock(:key)'),
        {'key': _FACTION_PROFILE_CAPACITY_LOCK_KEY},
    )


def _clip(value, limit: int) -> str:
    return str(value or '').strip()[:max(0, int(limit))]


def _field_limit() -> int:
    return max(600, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_FIELD_MAX_CHARS'),
        600,
    ))


def _text_list(value, *, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clip(item, item_chars)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalized_facets(value) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or '').strip().lower()
        if key not in FACTION_PROFILE_FACETS or not isinstance(raw_value, dict):
            continue
        summary = _clip(raw_value.get('summary'), _field_limit())
        visibility = str(raw_value.get('visibility') or '').strip().lower()
        if not summary or visibility not in FACTION_PROFILE_VISIBILITIES:
            continue
        result[key] = {'summary': summary, 'visibility': visibility}
    return result


def _profile_snapshot(
    profile: AdventurerS1FactionProfile,
    links: list[AdventurerS1FactionProfileThread],
) -> dict:
    return {
        'id': int(profile.id),
        'faction_key': profile.faction_key,
        'canonical_name': profile.canonical_name,
        'aliases': list(profile.aliases or []),
        'status': profile.status,
        'identity_summary': profile.identity_summary or '',
        'scope_keys': list(profile.scope_keys or []),
        'trigger_keywords': list(profile.trigger_keywords or []),
        'importance': int(profile.importance or 50),
        'facets': _normalized_facets(profile.facets),
        'revision': int(profile.revision or 1),
        'thread_links': [
            {
                'world_story_thread_id': int(link.world_story_thread_id),
                'relation_role': link.relation_role,
            }
            for link in sorted(links, key=lambda item: int(item.world_story_thread_id))
        ],
    }


def build_bootstrap_plan(existing_threads: list[dict]) -> dict | None:
    """Select one exact, currently unlinked faction thread for bounded cold start."""
    if (
        not _enabled()
        or not current_app.config.get(
            'ADVENTURER_S1_FACTION_PROFILE_AUTO_MAINTENANCE_ENABLED', True,
        )
        or not current_app.config.get(
            'ADVENTURER_S1_FACTION_PROFILE_BOOTSTRAP_ENABLED', True,
        )
    ):
        return None
    bootstrap_until_total = max(0, _coerce_int(
        current_app.config.get(
            'ADVENTURER_S1_FACTION_PROFILE_BOOTSTRAP_UNTIL_TOTAL',
        ),
        8,
    ))
    max_total = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_MAX_TOTAL'),
        200,
    ))
    bootstrap_until_total = min(bootstrap_until_total, max_total)
    if bootstrap_until_total <= 0:
        return None
    profile_count = int(AdventurerS1FactionProfile.query.count())
    if profile_count >= bootstrap_until_total:
        return None

    candidate_ids = list(dict.fromkeys(
        _positive_db_integer(row.get('id'))
        for row in existing_threads
        if isinstance(row, dict)
        and str(row.get('thread_type') or '').strip().lower() == 'faction'
        and _positive_db_integer(row.get('id')) > 0
    ))
    if not candidate_ids:
        return None
    linked_thread_ids = {
        int(thread_id)
        for (thread_id,) in (
            AdventurerS1FactionProfileThread.query
            .with_entities(
                AdventurerS1FactionProfileThread.world_story_thread_id,
            )
            .filter(
                AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                    candidate_ids,
                ),
            )
            .all()
        )
    }
    rows = (
        AdventurerS1WorldStoryThread.query
        .filter(
            AdventurerS1WorldStoryThread.id.in_(candidate_ids),
            AdventurerS1WorldStoryThread.thread_type == 'faction',
        )
        .all()
    )
    eligible = [
        row for row in rows
        if int(row.id) not in linked_thread_ids
        and _clip(row.title, 120)
        and _clip(row.summary, _field_limit())
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda row: (
        _BOOTSTRAP_STATUS_PRIORITY.get(str(row.status or '').strip().lower(), 9),
        -int(row.closure_debt or 0),
        -int(row.weight or 0),
        -(row.updated_at.timestamp() if row.updated_at else 0),
        int(row.id),
    ))
    selected = eligible[0]
    target = {
        'id': int(selected.id),
        'revision': int(selected.revision or 1),
        'title': _clip(selected.title, 120),
        'thread_type': 'faction',
        'status': str(selected.status or '').strip().lower(),
        'base_dungeon': _clip(selected.base_dungeon, 100),
        'scope_keys': _text_list(
            selected.scope_keys,
            limit=8,
            item_chars=80,
        ),
        'trigger_keywords': _text_list(
            selected.trigger_keywords,
            limit=10,
            item_chars=50,
        ),
        'summary': _clip(selected.summary, _field_limit()),
        'next_reveal_hint': _clip(selected.next_reveal_hint, _field_limit()),
        'power_tier': _clip(selected.power_tier, 30),
        'power_profile': _clip(selected.power_profile, _field_limit()),
    }
    return {
        'prompt_context': {
            'target_world_story_thread': target,
            'max_new_profiles': min(
                MAX_FACTION_PROFILE_UPDATE_ITEMS,
                bootstrap_until_total - profile_count,
            ),
        },
        'source_override': {
            'expected_revision': int(selected.revision or 1),
            'thread_title': _clip(selected.title, 120),
            'source_candidate_ids': list(dict.fromkeys(
                _positive_db_integer(value)
                for value in (selected.source_candidate_ids or [])
                if _positive_db_integer(value) > 0
            ))[-50:],
            'source_log_id': (
                _positive_db_integer(selected.last_seen_log_id) or None
            ),
            'max_new_profiles': min(
                MAX_FACTION_PROFILE_UPDATE_ITEMS,
                bootstrap_until_total - profile_count,
            ),
            'bootstrap_until_total': bootstrap_until_total,
            'require_unlinked': True,
        },
    }


def apply_maintenance_updates(
    raw_updates,
    *,
    candidate_ids: list[int],
    last_seen_log_id: int | None,
    source_overrides_by_thread_id: dict[int, dict] | None = None,
) -> dict:
    """Apply independent dossier rows; invalid/stale rows are diagnostic-only."""
    result = {
        'created': 0,
        'updated': 0,
        'linked': 0,
        'history_rows': 0,
        'skipped': 0,
        'diagnostics': [],
    }
    if not _enabled() or not current_app.config.get(
        'ADVENTURER_S1_FACTION_PROFILE_AUTO_MAINTENANCE_ENABLED', True,
    ):
        return result
    raw_updates, normalization_diagnostics = normalize_faction_profile_updates(
        raw_updates,
    )
    result['diagnostics'].extend(normalization_diagnostics)
    if not raw_updates:
        return result

    raw_target_counts: dict[tuple[str, object], int] = {}
    for raw in raw_updates:
        if not isinstance(raw, dict):
            continue
        raw_profile_id = _positive_db_integer(raw.get('id'))
        raw_name = _clip(raw.get('canonical_name'), 160).casefold()
        raw_target = (
            ('id', raw_profile_id)
            if raw_profile_id > 0 else ('name', raw_name)
        )
        if raw_profile_id > 0 or raw_name:
            raw_target_counts[raw_target] = raw_target_counts.get(raw_target, 0) + 1
    duplicate_targets = {
        target for target, count in raw_target_counts.items() if count > 1
    }
    reported_duplicate_targets: set[tuple[str, object]] = set()

    new_names = {
        _clip(raw.get('canonical_name'), 160)
        for raw in raw_updates if isinstance(raw, dict)
        and _positive_db_integer(raw.get('id')) <= 0
        and _clip(raw.get('canonical_name'), 160)
    }
    existing_ids = {
        _positive_db_integer(raw.get('id'))
        for raw in raw_updates if isinstance(raw, dict)
        if _positive_db_integer(raw.get('id')) > 0
    }
    source_overrides: dict[int, dict] = {}
    for raw_thread_id, raw_override in (
        source_overrides_by_thread_id or {}
    ).items():
        thread_id = _positive_db_integer(raw_thread_id)
        if thread_id <= 0 or not isinstance(raw_override, dict):
            continue
        source_overrides[thread_id] = {
            'expected_revision': _positive_db_integer(
                raw_override.get('expected_revision')
            ),
            'thread_title': _clip(raw_override.get('thread_title'), 120),
            'source_candidate_ids': list(dict.fromkeys(
                _positive_db_integer(value)
                for value in (raw_override.get('source_candidate_ids') or [])
                if _positive_db_integer(value) > 0
            ))[-50:],
            'source_log_id': (
                _positive_db_integer(raw_override.get('source_log_id')) or None
            ),
            'max_new_profiles': max(0, _coerce_int(
                raw_override.get('max_new_profiles'),
                0,
            )),
            'bootstrap_until_total': max(0, _coerce_int(
                raw_override.get('bootstrap_until_total'),
                0,
            )),
            'require_unlinked': bool(raw_override.get('require_unlinked')),
        }
    cold_update_indexes_by_thread: dict[int, list[int]] = {
        thread_id: [] for thread_id in source_overrides
    }
    for update_index, raw_update in enumerate(raw_updates):
        if (
            not isinstance(raw_update, dict)
            or _positive_db_integer(raw_update.get('id')) > 0
        ):
            continue
        for thread_id, override in source_overrides.items():
            if any(
                isinstance(link, dict)
                and (
                    _positive_db_integer(link.get('thread_id')) == thread_id
                    or (
                        _positive_db_integer(link.get('thread_id')) <= 0
                        and _clip(link.get('thread_title'), 120)
                        == override['thread_title']
                    )
                )
                for link in (raw_update.get('thread_links') or [])
            ):
                cold_update_indexes_by_thread[thread_id].append(update_index)
    cold_update_indexes = {
        update_index
        for indexes in cold_update_indexes_by_thread.values()
        for update_index in indexes
    }

    requested_thread_ids: set[int] = set()
    requested_thread_titles: set[str] = set()
    for raw in raw_updates:
        if not isinstance(raw, dict):
            continue
        for link in raw.get('thread_links') or []:
            if not isinstance(link, dict):
                continue
            thread_id = _positive_db_integer(link.get('thread_id'))
            thread_title = _clip(link.get('thread_title'), 120)
            if thread_id > 0:
                requested_thread_ids.add(thread_id)
            elif thread_title:
                requested_thread_titles.add(thread_title)
    requested_thread_ids.update(
        thread_id
        for thread_id, indexes in cold_update_indexes_by_thread.items()
        if indexes
    )
    thread_clauses = []
    if requested_thread_ids:
        thread_clauses.append(AdventurerS1WorldStoryThread.id.in_(requested_thread_ids))
    if requested_thread_titles:
        thread_clauses.append(
            AdventurerS1WorldStoryThread.title.in_(requested_thread_titles)
        )
    thread_rows = (
        AdventurerS1WorldStoryThread.query
        .filter(or_(*thread_clauses))
        .order_by(AdventurerS1WorldStoryThread.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
        if thread_clauses else []
    )
    threads_by_id = {int(row.id): row for row in thread_rows}
    threads_by_title = {str(row.title): row for row in thread_rows}

    # 世界线维护可能在进入本服务前已经锁住目标行，因此所有势力档案写路径
    # 统一保持 world_thread -> global_capacity -> profile -> link 的锁序。
    # 普通已有档案更新也会取得同一 world_thread 行锁，冷启动可在锁内重读
    # revision 与既有关联，避免只靠进程内状态或创建容量锁判断。
    if new_names:
        _lock_creation_capacity()

    profiles = (
        AdventurerS1FactionProfile.query
        .filter(AdventurerS1FactionProfile.id.in_(existing_ids))
        .order_by(AdventurerS1FactionProfile.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
        if existing_ids else []
    )
    profiles_by_id = {int(profile.id): profile for profile in profiles}
    existing_links = (
        AdventurerS1FactionProfileThread.query
        .filter(AdventurerS1FactionProfileThread.faction_profile_id.in_(existing_ids))
        .order_by(
            AdventurerS1FactionProfileThread.faction_profile_id.asc(),
            AdventurerS1FactionProfileThread.world_story_thread_id.asc(),
        )
        .with_for_update()
        .populate_existing()
        .all()
        if existing_ids else []
    )
    links_by_profile: dict[int, dict[int, AdventurerS1FactionProfileThread]] = {
        profile_id: {} for profile_id in existing_ids
    }
    for link in existing_links:
        links_by_profile.setdefault(int(link.faction_profile_id), {})[
            int(link.world_story_thread_id)
        ] = link

    occupied_names = {
        row.canonical_name: int(row.id)
        for row in (
            AdventurerS1FactionProfile.query
            .filter(AdventurerS1FactionProfile.canonical_name.in_(new_names))
            .with_for_update()
            .all()
            if new_names else []
        )
    }
    max_total = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_MAX_TOTAL'),
        200,
    ))
    total_count = int(AdventurerS1FactionProfile.query.count())
    default_candidate_ids = list(dict.fromkeys(
        _positive_db_integer(value)
        for value in candidate_ids if _positive_db_integer(value) > 0
    ))[-50:]
    prelinked_override_thread_ids = {
        int(thread_id)
        for (thread_id,) in (
            AdventurerS1FactionProfileThread.query
            .with_entities(
                AdventurerS1FactionProfileThread.world_story_thread_id,
            )
            .filter(
                AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                    list(source_overrides),
                ),
            )
            .all()
            if source_overrides else []
        )
    }
    overflow_override_indexes: set[int] = set()
    cold_capacity_checked = False
    seen_targets: set[tuple[str, object]] = set()
    processing_order = [
        (index, raw)
        for index, raw in enumerate(raw_updates)
        if index not in cold_update_indexes
    ] + [
        (index, raw)
        for index, raw in enumerate(raw_updates)
        if index in cold_update_indexes
    ]

    for index, raw in processing_order:
        if index in cold_update_indexes and not cold_capacity_checked:
            cold_capacity_checked = True
            for thread_id, override in source_overrides.items():
                matching_indexes = cold_update_indexes_by_thread.get(thread_id, [])
                available_slots = override['max_new_profiles']
                if override['bootstrap_until_total'] > 0:
                    available_slots = min(
                        available_slots,
                        max(
                            0,
                            override['bootstrap_until_total'] - total_count,
                        ),
                    )
                if len(matching_indexes) > available_slots:
                    overflow_override_indexes.update(matching_indexes)
                    result['diagnostics'].append(
                        '势力档案冷启动提案超过本轮剩余名额，'
                        '已丢弃全部冷启动项且不重试 AI'
                    )
        path = f'faction_profile_updates[{index}]'
        if not isinstance(raw, dict):
            result['skipped'] += 1
            result['diagnostics'].append(f'{path} 不是对象，已跳过')
            continue
        profile_id = _positive_db_integer(raw.get('id'))
        canonical_name = _clip(raw.get('canonical_name'), 160)
        item_candidate_ids = default_candidate_ids
        item_source_log_id = last_seen_log_id
        target_key = ('id', profile_id) if profile_id > 0 else ('name', canonical_name.casefold())
        if target_key in duplicate_targets:
            result['skipped'] += 1
            if target_key not in reported_duplicate_targets:
                result['diagnostics'].append(
                    f'{path} 与同批其它项指向同一档案，已丢弃全部冲突项'
                )
                reported_duplicate_targets.add(target_key)
            continue
        if target_key in seen_targets:
            result['skipped'] += 1
            result['diagnostics'].append(f'{path} 与同批另一项指向同一档案，已跳过')
            continue
        seen_targets.add(target_key)

        resolved_link_candidates: list[tuple[AdventurerS1WorldStoryThread, str]] = []
        for raw_link in raw.get('thread_links') or []:
            if not isinstance(raw_link, dict):
                continue
            thread = None
            thread_id = _positive_db_integer(raw_link.get('thread_id'))
            if thread_id > 0:
                thread = threads_by_id.get(thread_id)
            else:
                thread = threads_by_title.get(_clip(raw_link.get('thread_title'), 120))
            if not thread or thread.thread_type != 'faction':
                result['diagnostics'].append(
                    f'{path} 的世界线关联不存在或不是 faction 类型，已忽略该关联'
                )
                continue
            role = str(raw_link.get('relation_role') or 'involved').strip().lower()
            if role not in FACTION_PROFILE_THREAD_ROLES:
                role = 'involved'
            resolved_link_candidates.append((thread, role))
        resolved_link_counts: dict[int, int] = {}
        for thread, _role in resolved_link_candidates:
            resolved_link_counts[int(thread.id)] = (
                resolved_link_counts.get(int(thread.id), 0) + 1
            )
        duplicate_resolved_thread_ids = {
            thread_id for thread_id, count in resolved_link_counts.items()
            if count > 1
        }
        if duplicate_resolved_thread_ids:
            result['diagnostics'].append(
                f'{path} 的多个关联解析到同一世界线，已丢弃全部冲突关联'
            )
        resolved_links = [
            (thread, role)
            for thread, role in resolved_link_candidates
            if int(thread.id) not in duplicate_resolved_thread_ids
        ]

        is_new = profile_id <= 0
        if is_new and index in overflow_override_indexes:
            result['skipped'] += 1
            continue
        raw_source_override_ids = {
            thread_id
            for thread_id in source_overrides
            if any(
                isinstance(link, dict)
                and _positive_db_integer(link.get('thread_id')) == thread_id
                for link in (raw.get('thread_links') or [])
            )
        }
        title_source_override_ids = {
            thread_id
            for thread_id, override in source_overrides.items()
            if override['thread_title']
            and any(
                isinstance(link, dict)
                and _positive_db_integer(link.get('thread_id')) <= 0
                and _clip(link.get('thread_title'), 120)
                == override['thread_title']
                for link in (raw.get('thread_links') or [])
            )
        }
        if is_new and title_source_override_ids - raw_source_override_ids:
            result['skipped'] += 1
            result['diagnostics'].append(
                f'{path} 的冷启动目标必须精确使用冻结 thread_id，已跳过且不重试 AI'
            )
            continue
        matching_source_overrides = [
            (int(thread.id), source_overrides[int(thread.id)])
            for thread, _role in resolved_links
            if int(thread.id) in source_overrides
        ]
        if is_new and len(matching_source_overrides) > 1:
            result['skipped'] += 1
            result['diagnostics'].append(
                f'{path} 同时绑定多个冷启动来源，已跳过且不重试 AI'
            )
            continue
        if is_new and matching_source_overrides:
            source_thread_id, source_override = matching_source_overrides[0]
            source_thread = threads_by_id.get(source_thread_id)
            if {
                int(thread.id) for thread, _role in resolved_links
            } != {source_thread_id}:
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} 的冷启动提案关联了目标外世界线，已跳过且不重试 AI'
                )
                continue
            if (
                source_override['require_unlinked']
                and source_thread_id in prelinked_override_thread_ids
            ):
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} 的冷启动世界线已由其它档案接管，已跳过且不重试 AI'
                )
                continue
            if (
                not source_thread
                or source_override['expected_revision'] <= 0
                or int(source_thread.revision or 1)
                != source_override['expected_revision']
            ):
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} 的冷启动世界线 revision 已变化，已跳过且不重试 AI'
                )
                continue
            item_candidate_ids = source_override['source_candidate_ids']
            item_source_log_id = source_override['source_log_id']
        if is_new:
            if not canonical_name or not _clip(raw.get('identity_summary'), _field_limit()):
                result['skipped'] += 1
                result['diagnostics'].append(f'{path} 缺少新档案稳定身份，已跳过')
                continue
            if not resolved_links:
                result['skipped'] += 1
                result['diagnostics'].append(f'{path} 没有精确 faction 世界线关联，已跳过')
                continue
            if canonical_name in occupied_names:
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} 的 canonical_name 已存在；必须用准确 id/revision 更新，已跳过'
                )
                continue
            if total_count >= max_total:
                result['skipped'] += 1
                result['diagnostics'].append(f'{path} 超过势力档案总量上限，已跳过')
                continue
            if matching_source_overrides:
                bootstrap_until_total = matching_source_overrides[0][1][
                    'bootstrap_until_total'
                ]
                if (
                    bootstrap_until_total > 0
                    and total_count >= bootstrap_until_total
                ):
                    result['skipped'] += 1
                    result['diagnostics'].append(
                        f'{path} 到达势力档案冷启动总量边界，'
                        '已跳过且不重试 AI'
                    )
                    continue
            profile = None
        else:
            profile = profiles_by_id.get(profile_id)
            if not profile:
                result['skipped'] += 1
                result['diagnostics'].append(f'{path} 指向不存在的档案，已跳过')
                continue
            expected_revision = _coerce_int(raw.get('source_revision'), 0)
            if expected_revision != int(profile.revision or 1):
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} revision 已过期，已跳过该项且不重试 AI'
                )
                continue
            if canonical_name and canonical_name != profile.canonical_name:
                result['skipped'] += 1
                result['diagnostics'].append(
                    f'{path} 试图通过维护输出改名，已跳过该项'
                )
                continue

        item_linked = 0
        profile_links: dict[int, AdventurerS1FactionProfileThread] = {}
        try:
            # 每个档案项覆盖 profile/link/history 的完整独立 savepoint。
            # 任一 flush 或约束失败只回滚该项，合法 sibling 仍可继续落库。
            with db_manager.begin_nested():
                if is_new:
                    profile = AdventurerS1FactionProfile(
                        faction_key=f'faction:{uuid.uuid4().hex}',
                        canonical_name=canonical_name,
                        identity_summary=_clip(
                            raw.get('identity_summary'),
                            _field_limit(),
                        ),
                        status=(
                            str(raw.get('status') or 'active').strip().lower()
                            if str(raw.get('status') or 'active').strip().lower()
                            in FACTION_PROFILE_STATUSES else 'active'
                        ),
                        revision=1,
                    )
                    added, add_error = db_manager.add(profile, flush=False)
                    if not added:
                        raise RuntimeError(add_error or '档案加入会话失败')
                    profile_flushed, profile_flush_error = db_manager.flush()
                    if not profile_flushed:
                        raise RuntimeError(
                            profile_flush_error or '档案唯一约束落库失败'
                        )
                    changed = True
                    profile_links = {}
                else:
                    changed = False
                    profile_links = dict(links_by_profile.get(int(profile.id), {}))

                for field, value in (
                    (
                        'identity_summary',
                        _clip(raw.get('identity_summary'), _field_limit()),
                    ),
                    ('status', str(raw.get('status') or '').strip().lower()),
                ):
                    if field not in raw or not value:
                        continue
                    if field == 'status' and value not in FACTION_PROFILE_STATUSES:
                        continue
                    if getattr(profile, field) != value:
                        setattr(profile, field, value)
                        changed = True
                for field, limit, item_chars in (
                    ('aliases', 8, 160),
                    ('scope_keys', 8, 80),
                    ('trigger_keywords', 10, 50),
                ):
                    if field not in raw:
                        continue
                    value = _text_list(
                        raw.get(field),
                        limit=limit,
                        item_chars=item_chars,
                    )
                    if list(getattr(profile, field) or []) != value:
                        setattr(profile, field, value)
                        changed = True
                if 'importance' in raw:
                    importance = max(
                        1,
                        min(100, _coerce_int(raw.get('importance'), 50)),
                    )
                    if int(profile.importance or 50) != importance:
                        profile.importance = importance
                        changed = True

                facets = _normalized_facets(profile.facets)
                next_facets = dict(facets)
                for raw_facet in raw.get('facet_updates') or []:
                    if not isinstance(raw_facet, dict):
                        continue
                    facet = str(raw_facet.get('facet') or '').strip().lower()
                    visibility = str(
                        raw_facet.get('visibility') or ''
                    ).strip().lower()
                    summary = _clip(raw_facet.get('summary'), _field_limit())
                    if (
                        facet not in FACTION_PROFILE_FACETS
                        or visibility not in FACTION_PROFILE_VISIBILITIES
                        or not summary
                    ):
                        continue
                    next_facets[facet] = {
                        'summary': summary,
                        'visibility': visibility,
                    }
                if next_facets != facets:
                    profile.facets = next_facets
                    changed = True

                for thread, role in resolved_links:
                    link = profile_links.get(int(thread.id))
                    if not link:
                        link = AdventurerS1FactionProfileThread(
                            faction_profile_id=int(profile.id),
                            world_story_thread_id=int(thread.id),
                            relation_role=role,
                            source_log_id=item_source_log_id,
                        )
                        linked, link_error = db_manager.add(link, flush=False)
                        if not linked:
                            raise RuntimeError(
                                link_error or '档案世界线关联加入会话失败'
                            )
                        profile_links[int(thread.id)] = link
                        item_linked += 1
                        changed = True
                    elif link.relation_role != role:
                        link.relation_role = role
                        link.source_log_id = (
                            item_source_log_id or link.source_log_id
                        )
                        changed = True

                if not changed:
                    raise _NoFactionProfileChange
                if not is_new:
                    profile.revision = int(profile.revision or 1) + 1
                profile.source_candidate_ids = list(dict.fromkeys(
                    list(profile.source_candidate_ids or []) + item_candidate_ids
                ))[-50:]
                profile.first_seen_log_id = (
                    profile.first_seen_log_id or item_source_log_id
                )
                profile.last_seen_log_id = (
                    item_source_log_id or profile.last_seen_log_id
                )
                profile.updated_at = _portable_utc_now()
                profile_flushed, profile_flush_error = db_manager.flush()
                if not profile_flushed:
                    raise RuntimeError(
                        profile_flush_error or '势力档案当前态落库失败'
                    )
                history = AdventurerS1FactionProfileHistory(
                    faction_profile_id=int(profile.id),
                    revision=int(profile.revision or 1),
                    snapshot=_profile_snapshot(
                        profile,
                        list(profile_links.values()),
                    ),
                    change_summary=_clip(
                        raw.get('change_summary'),
                        _field_limit(),
                    ),
                    source_candidate_ids=item_candidate_ids,
                    source_log_id=item_source_log_id,
                )
                history_added, history_error = db_manager.add(
                    history,
                    flush=False,
                )
                if not history_added:
                    raise RuntimeError(
                        history_error or '势力档案历史加入会话失败'
                    )
                history_flushed, history_flush_error = db_manager.flush()
                if not history_flushed:
                    raise RuntimeError(
                        history_flush_error or '势力档案历史落库失败'
                    )
        except _NoFactionProfileChange:
            result['skipped'] += 1
            result['diagnostics'].append(
                f'{path} 未形成当前态变化，已跳过'
            )
            continue
        except Exception as exc:
            result['skipped'] += 1
            result['diagnostics'].append(
                f'{path} 辅助落库异常，已回滚该项且不重试 AI'
            )
            logger.warning(
                'S1 势力档案单项落库失败 path=%s: %s',
                path,
                exc,
                exc_info=True,
            )
            continue

        profile_id = int(profile.id)
        profiles_by_id[profile_id] = profile
        links_by_profile[profile_id] = profile_links
        if index not in cold_update_indexes:
            prelinked_override_thread_ids.update(
                thread_id
                for thread_id in profile_links
                if thread_id in source_overrides
            )
        result['linked'] += item_linked
        result['history_rows'] += 1
        if is_new:
            occupied_names[canonical_name] = profile_id
            total_count += 1
            result['created'] += 1
        else:
            result['updated'] += 1
    return result


def build_maintenance_context(thread_ids: list[int], *, target_dungeon: str | None = None) -> list[dict]:
    """Return bounded full dossier state, including restricted facets, to maintenance AI."""
    if not _enabled() or not thread_ids:
        return []
    normalized_thread_ids = list(dict.fromkeys(
        _positive_db_integer(value)
        for value in thread_ids if _positive_db_integer(value) > 0
    ))
    links = AdventurerS1FactionProfileThread.query.filter(
        AdventurerS1FactionProfileThread.world_story_thread_id.in_(normalized_thread_ids),
    ).all()
    profile_ids = list(dict.fromkeys(int(link.faction_profile_id) for link in links))
    limit = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_MAINTENANCE_LIMIT'),
        8,
    ))
    profile_query = AdventurerS1FactionProfile.query.filter(AdventurerS1FactionProfile.id.in_(profile_ids))
    if target_dungeon:
        from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
        profile_query = profile_query.filter(S1CampaignWorldRepository.entity_scope(AdventurerS1FactionProfile, target_dungeon))
    profiles = (
        profile_query
        .order_by(
            AdventurerS1FactionProfile.importance.desc(),
            AdventurerS1FactionProfile.id.asc(),
        )
        .limit(limit)
        .all()
        if profile_ids else []
    )
    selected_ids = {int(profile.id) for profile in profiles}
    all_links = (
        AdventurerS1FactionProfileThread.query
        .filter(AdventurerS1FactionProfileThread.faction_profile_id.in_(selected_ids))
        .order_by(AdventurerS1FactionProfileThread.world_story_thread_id.asc())
        .all()
        if selected_ids else []
    )
    links_by_profile: dict[int, list[AdventurerS1FactionProfileThread]] = {
        profile_id: [] for profile_id in selected_ids
    }
    for link in all_links:
        links_by_profile.setdefault(int(link.faction_profile_id), []).append(link)
    snapshots = [
        _profile_snapshot(profile, links_by_profile.get(int(profile.id), []))
        for profile in profiles
    ]
    total_chars = max(2000, _coerce_int(
        current_app.config.get(
            'ADVENTURER_S1_FACTION_PROFILE_MAINTENANCE_TOTAL_CHARS',
        ),
        12000,
    ))
    result: list[dict] = []
    used_chars = 0
    for snapshot in snapshots:
        snapshot_chars = len(json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(',', ':'),
        ))
        if used_chars + snapshot_chars > total_chars:
            continue
        result.append(snapshot)
        used_chars += snapshot_chars
    return result


def build_name_registry() -> list[str]:
    """Global occupied identity registry for new-profile naming, never for fuzzy matching."""
    if not _enabled():
        return []
    limit = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_MAX_TOTAL'), 200,
    ))
    rows = (
        AdventurerS1FactionProfile.query
        .with_entities(
            AdventurerS1FactionProfile.canonical_name,
            AdventurerS1FactionProfile.aliases,
        )
        .order_by(AdventurerS1FactionProfile.id.asc())
        .limit(limit)
        .all()
    )
    name_limit = max(limit, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_NAME_REGISTRY_LIMIT'),
        320,
    ))
    total_chars = max(1000, _coerce_int(
        current_app.config.get(
            'ADVENTURER_S1_FACTION_PROFILE_NAME_REGISTRY_TOTAL_CHARS',
        ),
        8000,
    ))
    result: list[str] = []
    seen: set[str] = set()
    used_chars = 0
    for canonical_name, _aliases in rows:
        text = _clip(canonical_name, 160)
        key = text.casefold()
        if text and key not in seen:
            if used_chars + len(text) > total_chars:
                return result
            seen.add(key)
            result.append(text)
            used_chars += len(text)
            if len(result) >= name_limit:
                return result
    for _canonical_name, aliases in rows:
        for text in _text_list(aliases, limit=8, item_chars=160):
            key = text.casefold()
            if key in seen:
                continue
            if used_chars + len(text) > total_chars:
                return result
            seen.add(key)
            result.append(text)
            used_chars += len(text)
            if len(result) >= name_limit:
                return result
    return result


def build_prompt_entries(
    thread_ids: list[int],
    *,
    priority_profile_ids: set[int] | None = None,
) -> dict:
    """Build public/rumor-only dossier entries keyed by exact selected world thread."""
    empty = {'entries_by_thread_id': {}, 'profile_ids': [], 'states': []}
    if not _enabled() or not thread_ids:
        return empty
    normalized_thread_ids = list(dict.fromkeys(
        _positive_db_integer(value)
        for value in thread_ids if _positive_db_integer(value) > 0
    ))
    links = (
        AdventurerS1FactionProfileThread.query
        .filter(
            AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                normalized_thread_ids,
            ),
        )
        .order_by(AdventurerS1FactionProfileThread.world_story_thread_id.asc())
        .all()
    )
    profile_ids = list(dict.fromkeys(int(link.faction_profile_id) for link in links))
    limit = max(0, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_PROMPT_LIMIT'), 2,
    ))
    if not profile_ids or limit <= 0:
        return empty
    profiles = list(
        AdventurerS1FactionProfile.query
        .filter(AdventurerS1FactionProfile.id.in_(profile_ids))
        .all()
    )
    priority_ids = {
        _positive_db_integer(value)
        for value in (priority_profile_ids or set())
        if _positive_db_integer(value) > 0
    }
    profiles.sort(key=lambda profile: (
        0 if int(profile.id) in priority_ids else 1,
        -int(profile.importance or 50),
        int(profile.id),
    ))
    profiles = profiles[:limit]
    links_by_profile: dict[int, list[int]] = {}
    for link in links:
        links_by_profile.setdefault(int(link.faction_profile_id), []).append(
            int(link.world_story_thread_id)
        )
    total_chars = max(600, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_PROMPT_TOTAL_CHARS'),
        1800,
    ))
    facet_limit = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_PROMPT_FACET_LIMIT'),
        8,
    ))
    header_chars = 220
    used_chars = header_chars
    entries_by_thread_id: dict[int, list[dict]] = {}
    included_profile_ids: list[int] = []
    states: list[dict] = []
    for profile in profiles:
        linked_thread_ids = sorted(set(
            links_by_profile.get(int(profile.id), [])
        ))
        if not linked_thread_ids:
            continue
        lines = [
            (
                f'[faction_profile_id:{profile.id}] '
                f'[faction_key:{profile.faction_key}] '
                f'[profile_revision:{int(profile.revision or 1)}] '
                f'{profile.canonical_name}（{profile.status}）'
            ),
            (
                '未证实传闻·稳定身份：'
                if profile.status == 'rumored'
                else '公开稳定身份：'
            ) + (profile.identity_summary or ''),
        ]
        facets = _normalized_facets(profile.facets)
        included_facets: dict[str, dict[str, str]] = {}
        for facet in _FACET_PROMPT_ORDER:
            value = facets.get(facet)
            if not value or value['visibility'] not in {'public', 'rumor'}:
                continue
            if (
                facet == 'current_agenda'
                and (
                    profile.status in {'dormant', 'dissolved'}
                    or (
                        profile.status == 'rumored'
                        and value['visibility'] != 'rumor'
                    )
                )
            ):
                continue
            visibility_label = '公开当前态' if value['visibility'] == 'public' else '未证实传闻'
            line = f'{visibility_label}·{_FACET_LABELS[facet]}：{value["summary"]}'
            if len(included_facets) >= facet_limit:
                break
            if used_chars + len('\n'.join(lines + [line])) > total_chars:
                if facet == 'current_state':
                    # Current conditions can qualify every later capability,
                    # resource or agenda. Keep only identity if they cannot fit.
                    break
                # Keep facets intact; an oversized optional fact must not hide
                # later, shorter facts that still fit the same input budget.
                continue
            lines.append(line)
            included_facets[facet] = dict(value)
        prompt_block = '\n'.join(lines)
        if used_chars + len(prompt_block) > total_chars:
            continue
        used_chars += len(prompt_block)
        entry = {
            'faction_profile_id': int(profile.id),
            'faction_key': profile.faction_key,
            'profile_revision': int(profile.revision or 1),
            'linked_world_story_thread_ids': linked_thread_ids,
            'selection_prompt_block': lines[0],
            'prompt_block': prompt_block,
        }
        for thread_id in linked_thread_ids:
            entries_by_thread_id.setdefault(thread_id, []).append(entry)
        included_profile_ids.append(int(profile.id))
        states.append({
            'id': int(profile.id),
            'faction_key': profile.faction_key,
            'canonical_name': profile.canonical_name,
            'status': profile.status,
            'identity_summary': profile.identity_summary or '',
            'identity_visibility': (
                'rumor' if profile.status == 'rumored' else 'public'
            ),
            'revision': int(profile.revision or 1),
            'facets': included_facets,
            'linked_world_story_thread_ids': linked_thread_ids,
        })
    return {
        'entries_by_thread_id': entries_by_thread_id,
        'profile_ids': included_profile_ids,
        'states': states,
    }
