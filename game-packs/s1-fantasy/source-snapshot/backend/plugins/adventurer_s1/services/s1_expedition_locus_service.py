"""S1 单次探险局部域：后端冻结、Prompt 投影与世界线精确作用域。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from hashlib import sha256
from typing import Iterable

from flask import current_app
from sqlalchemy import or_, select

from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog,
    AdventurerS1NarrativeLease,
    AdventurerS1WorldActor,
    AdventurerS1WorldLocationState,
    AdventurerS1WorldStoryThread,
    AdventurerS1WorldStoryThreadActor,
)


EXPEDITION_LOCUS_LOG_FIELD = '_expedition_locus_snapshot'
EXPEDITION_LOCUS_SCHEMA_VERSION = 1
EXPEDITION_LOCUS_KIND_KNOWN_LOCATION = 'known_location'
EXPEDITION_LOCUS_KIND_NEW_FRONTIER = 'new_frontier'
LOCATION_SCOPE_KEY_PREFIX = 's1_location_state:'
EXPEDITION_LOCUS_SCOPE_KEY_PREFIX = 's1_expedition_locus:'


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def location_scope_key(location_id: object) -> str | None:
    parsed = _coerce_positive_int(location_id)
    return f'{LOCATION_SCOPE_KEY_PREFIX}{parsed}' if parsed else None


def extract_location_scope_keys(scope_keys: Iterable[object] | None) -> set[str]:
    """Return reserved local refs, including malformed refs so matching fails closed."""
    return {
        str(value).strip()
        for value in (scope_keys or [])
        if str(value or '').strip().startswith(LOCATION_SCOPE_KEY_PREFIX)
    }


def story_scope_matches_expedition_locus(
    scope_keys: Iterable[object] | None,
    expedition_locus_scope_key: str | None,
) -> bool:
    """Legacy callers pass None; an active frontier passes its transient scope key."""
    local_refs = extract_location_scope_keys(scope_keys)
    if not local_refs or expedition_locus_scope_key is None:
        return True
    return str(expedition_locus_scope_key).strip() in local_refs


def sanitize_story_location_scope_keys(
    scope_keys: Iterable[object] | None,
    *,
    allowed_location_scope_keys: Iterable[str] | None,
) -> tuple[list[str], list[str]]:
    """Drop ungranted reserved refs without rejecting an otherwise valid AI result."""
    allowed = set()
    for raw in allowed_location_scope_keys or []:
        value = str(raw or '').strip()
        if not value.startswith(LOCATION_SCOPE_KEY_PREFIX):
            continue
        if location_scope_key(value[len(LOCATION_SCOPE_KEY_PREFIX):]) == value:
            allowed.add(value)
    kept: list[str] = []
    rejected: list[str] = []
    for raw in scope_keys or []:
        value = str(raw or '').strip()
        if not value or value in kept:
            continue
        if value.startswith(EXPEDITION_LOCUS_SCOPE_KEY_PREFIX):
            rejected.append(value)
            continue
        if value.startswith(LOCATION_SCOPE_KEY_PREFIX) and value not in allowed:
            rejected.append(value)
            continue
        kept.append(value)
    return kept, rejected


def normalize_expedition_locus_snapshot(
    value: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> dict:
    if not isinstance(value, dict):
        return {}
    if value.get('schema_version') != EXPEDITION_LOCUS_SCHEMA_VERSION:
        return {}
    log_id = _coerce_positive_int(value.get('expedition_log_id'))
    if not log_id:
        return {}
    if expected_expedition_log_id and log_id != int(expected_expedition_log_id):
        return {}
    dungeon = str(value.get('target_dungeon') or '').strip()
    if not dungeon:
        return {}
    if expected_target_dungeon is not None and dungeon != str(expected_target_dungeon or '').strip():
        return {}
    kind = str(value.get('kind') or '').strip()
    captured_at = str(value.get('captured_at') or '').strip()
    if kind == EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
        location_id = _coerce_positive_int(value.get('location_id'))
        location_name = str(value.get('location_name') or '').strip()
        revision = value.get('selected_revision')
        if isinstance(revision, bool):
            return {}
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return {}
        expected_scope_key = location_scope_key(location_id)
        if (
            not location_id
            or not location_name
            or revision < 0
            or str(value.get('scope_key') or '').strip() != expected_scope_key
        ):
            return {}
        return {
            'schema_version': EXPEDITION_LOCUS_SCHEMA_VERSION,
            'expedition_log_id': log_id,
            'target_dungeon': dungeon,
            'kind': kind,
            'scope_key': expected_scope_key,
            'location_id': location_id,
            'location_name': location_name,
            'selected_revision': revision,
            'captured_at': captured_at,
        }
    if kind == EXPEDITION_LOCUS_KIND_NEW_FRONTIER:
        expected_scope_key = f'{EXPEDITION_LOCUS_SCOPE_KEY_PREFIX}{log_id}'
        if str(value.get('scope_key') or '').strip() != expected_scope_key:
            return {}
        return {
            'schema_version': EXPEDITION_LOCUS_SCHEMA_VERSION,
            'expedition_log_id': log_id,
            'target_dungeon': dungeon,
            'kind': kind,
            'scope_key': expected_scope_key,
            'location_id': None,
            'location_name': '',
            'selected_revision': None,
            'captured_at': captured_at,
        }
    return {}


def project_expedition_locus_for_ai(
    value: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> dict:
    """Expose only the spatial facts an AI stage needs; omit internal refs/timestamps."""
    locus = normalize_expedition_locus_snapshot(
        value,
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if not locus:
        return {}
    projected = {
        'target_dungeon': locus['target_dungeon'],
        'kind': locus['kind'],
    }
    if locus['kind'] == EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
        projected.update({
            'location_id': locus['location_id'],
            'location_name': locus['location_name'],
        })
    return projected


def load_continuation_location_ids(target_dungeon: str) -> set[int]:
    """Read bounded local continuation candidates; selection grants no write lease."""
    from .adventurer_s1_config_service import is_s1_independent_dungeon
    if is_s1_independent_dungeon(target_dungeon):
        return set()
    dungeon = str(target_dungeon or '').strip()
    if not dungeon or int(current_app.config.get(
        'ADVENTURER_S1_WORLD_STORY_THREADS_PROMPT_LIMIT', 1,
    ) or 0) <= 0:
        return set()
    rows = AdventurerS1WorldStoryThread.query.filter(
        AdventurerS1WorldStoryThread.base_dungeon == dungeon,
        AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing')),
        AdventurerS1WorldStoryThread.superseded_by_id.is_(None),
        or_(AdventurerS1WorldStoryThread.is_chain_head.is_(True),
            AdventurerS1WorldStoryThread.is_chain_head.is_(None)),
        # Published campaign fronts keep their own departure/knowledge contract.
        AdventurerS1WorldStoryThread.campaign_role.is_(None),
    ).order_by(
        AdventurerS1WorldStoryThread.closure_debt.desc(),
        AdventurerS1WorldStoryThread.injection_count.asc(),
        AdventurerS1WorldStoryThread.weight.desc(),
        AdventurerS1WorldStoryThread.id.asc(),
    ).with_entities(
        AdventurerS1WorldStoryThread.id, AdventurerS1WorldStoryThread.scope_keys,
    ).limit(128).all()
    scopes_by_thread = {
        row.id: extract_location_scope_keys(row.scope_keys) for row in rows
        if extract_location_scope_keys(row.scope_keys)
    }
    if not scopes_by_thread:
        return set()
    links = AdventurerS1WorldStoryThreadActor.query.join(
        AdventurerS1WorldActor,
        AdventurerS1WorldActor.id == AdventurerS1WorldStoryThreadActor.actor_id,
    ).filter(
        AdventurerS1WorldStoryThreadActor.world_story_thread_id.in_(scopes_by_thread),
    ).with_entities(
        AdventurerS1WorldStoryThreadActor.world_story_thread_id,
        AdventurerS1WorldStoryThreadActor.actor_id,
        AdventurerS1WorldStoryThreadActor.involvement,
        AdventurerS1WorldStoryThreadActor.lease_required,
        AdventurerS1WorldActor.status,
        AdventurerS1WorldActor.capability_revision,
        AdventurerS1WorldActor.capability_profile,
    ).all()
    excluded = {
        link.world_story_thread_id for link in links
        if link.status == 'active' and (
            link.involvement != 'direct' or not link.lease_required
            or not link.capability_profile or not link.capability_revision
        )
    }
    if current_app.config.get('ADVENTURER_S1_NARRATIVE_LEASE_ENABLED', True):
        active_owners = select(AdventurerS1ExpeditionLog.id).where(
            AdventurerS1ExpeditionLog.status.in_(('generating', 'in_progress')),
        )
        leases = AdventurerS1NarrativeLease.query.filter(
            AdventurerS1NarrativeLease.status == 'active',
            or_(AdventurerS1NarrativeLease.expires_at > _portable_utc_now(),
                AdventurerS1NarrativeLease.expedition_log_id.in_(active_owners)),
            or_(AdventurerS1NarrativeLease.world_story_thread_id.in_(scopes_by_thread),
                AdventurerS1NarrativeLease.world_actor_id.in_({link.actor_id for link in links})),
        ).with_entities(
            AdventurerS1NarrativeLease.world_story_thread_id,
            AdventurerS1NarrativeLease.world_actor_id,
        ).all()
        excluded.update(lease.world_story_thread_id for lease in leases)
        busy_actors = {lease.world_actor_id for lease in leases if lease.world_actor_id}
        excluded.update(link.world_story_thread_id for link in links if link.actor_id in busy_actors)
    return {
        int(scope[len(LOCATION_SCOPE_KEY_PREFIX):])
        for thread_id, scopes in scopes_by_thread.items() if thread_id not in excluded
        for scope in scopes
        if location_scope_key(scope[len(LOCATION_SCOPE_KEY_PREFIX):]) == scope
    }


def build_expedition_locus_snapshot(
    expedition_log_id: int,
    target_dungeon: str,
    location_snapshot: dict | None,
    *,
    new_frontier_percent: int,
    preferred_location_ids: Iterable[int] | None = None,
) -> dict:
    """Choose one bounded domain deterministically; this function performs no writes."""
    from .adventurer_s1_config_service import is_s1_independent_dungeon
    if is_s1_independent_dungeon(target_dungeon):
        return {}
    log_id = _coerce_positive_int(expedition_log_id)
    dungeon = str(target_dungeon or '').strip()
    if not log_id or not dungeon:
        return {}
    candidates = []
    for item in (
        location_snapshot.get('locations')
        if isinstance(location_snapshot, dict) else []
    ) or []:
        if not isinstance(item, dict) or item.get('_write_available') is False:
            continue
        location_id = _coerce_positive_int(item.get('location_id'))
        location_name = str(item.get('location_name') or '').strip()
        revision = item.get('revision')
        if not location_id or not location_name or isinstance(revision, bool):
            continue
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            continue
        if revision < 0:
            continue
        candidates.append({
            'location_id': location_id,
            'location_name': location_name,
            'revision': revision,
        })
    candidates.sort(key=lambda item: int(item['location_id']))
    digest = int.from_bytes(
        sha256(f's1-expedition-locus:v1:{dungeon}:{log_id}'.encode('utf-8')).digest()[:8],
        'big',
    )
    frontier_percent = max(0, min(100, int(new_frontier_percent or 0)))
    use_frontier = not candidates or digest % 100 < frontier_percent
    captured_at = _portable_utc_now().isoformat()
    if use_frontier:
        return {
            'schema_version': EXPEDITION_LOCUS_SCHEMA_VERSION,
            'expedition_log_id': log_id,
            'target_dungeon': dungeon,
            'kind': EXPEDITION_LOCUS_KIND_NEW_FRONTIER,
            'scope_key': f'{EXPEDITION_LOCUS_SCOPE_KEY_PREFIX}{log_id}',
            'location_id': None,
            'location_name': '',
            'selected_revision': None,
            'captured_at': captured_at,
        }
    preferred_ids = set(preferred_location_ids or ())
    continuation_candidates = [item for item in candidates if item['location_id'] in preferred_ids]
    pool = continuation_candidates or candidates
    selected = pool[(digest // 100) % len(pool)]
    return {
        'schema_version': EXPEDITION_LOCUS_SCHEMA_VERSION,
        'expedition_log_id': log_id,
        'target_dungeon': dungeon,
        'kind': EXPEDITION_LOCUS_KIND_KNOWN_LOCATION,
        'scope_key': location_scope_key(selected['location_id']),
        'location_id': selected['location_id'],
        'location_name': selected['location_name'],
        'selected_revision': selected['revision'],
        'captured_at': captured_at,
    }


def load_expedition_locus_snapshot(
    expedition_log_id: int | None,
    target_dungeon: str,
) -> dict:
    log_id = _coerce_positive_int(expedition_log_id)
    if not log_id:
        return {}
    log = (
        AdventurerS1ExpeditionLog.query
        .with_entities(AdventurerS1ExpeditionLog.full_log)
        .filter_by(id=log_id)
        .first()
    )
    full_log = log[0] if log and isinstance(log[0], dict) else {}
    return normalize_expedition_locus_snapshot(
        full_log.get(EXPEDITION_LOCUS_LOG_FIELD),
        expected_target_dungeon=target_dungeon,
        expected_expedition_log_id=log_id,
    )


def _project_location_item(item: dict | None) -> dict | None:
    if not isinstance(item, dict):
        return None
    location_id = _coerce_positive_int(item.get('location_id'))
    location_name = str(item.get('location_name') or '').strip()
    revision = item.get('revision')
    if not location_id or not location_name or isinstance(revision, bool):
        return None
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        return None
    if revision < 0:
        return None
    return {
        'location_id': location_id,
        'location_name': location_name,
        'revision': revision,
        'current_state': str(item.get('current_state') or '').strip(),
        'public_risk_notice': str(item.get('public_risk_notice') or '').strip(),
    }


def project_location_snapshot_to_locus(
    location_snapshot: dict,
    expedition_locus: dict,
) -> dict:
    """Project current authority to one locus while keeping the macro dungeon identity."""
    locus = normalize_expedition_locus_snapshot(expedition_locus)
    if not locus:
        return dict(location_snapshot or {})
    dungeon = locus['target_dungeon']
    if str((location_snapshot or {}).get('target_dungeon') or '').strip() != dungeon:
        return dict(location_snapshot or {})
    selected_item = None
    if locus['kind'] == EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
        for item in (location_snapshot.get('locations') or []):
            if isinstance(item, dict) and item.get('location_id') == locus['location_id']:
                selected_item = _project_location_item(item)
                break
        if selected_item is None:
            row = (
                AdventurerS1WorldLocationState.query
                .filter_by(id=locus['location_id'], target_dungeon=dungeon)
                .first()
            )
            if row:
                selected_item = {
                    'location_id': int(row.id),
                    'location_name': row.canonical_name,
                    'revision': int(row.revision or 0),
                    'current_state': row.current_state or '',
                    'public_risk_notice': row.public_risk_notice or '',
                }
    projected = {
        'target_dungeon': dungeon,
        'is_explorable': location_snapshot.get('is_explorable'),
        'identity_is_persistent': location_snapshot.get('identity_is_persistent') is True,
        'locations': [selected_item] if selected_item else [],
        'captured_at': location_snapshot.get('captured_at'),
    }
    from plugins.adventurer_s1.services.s1_world_location_state_service import (
        S1WorldLocationStateService,
    )
    projected['prompt_block'] = S1WorldLocationStateService.render_prompt_block(
        projected,
        locus_prompt_block=_render_locus_prompt_block(locus),
    )
    return projected


def _render_locus_prompt_block(locus: dict) -> str:
    dungeon = locus['target_dungeon']
    if locus['kind'] == EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
        anchor = (
            f"本队本轮落点固定在已登记地点「{locus['location_name']}」及其因果相连的邻近场景；"
            '未列入本次快照的同地牢地点不与本场默认共址。'
        )
    else:
        anchor = (
            '本队本轮落点是一处尚未登记的局部探索域；它可以包含若干因果相连的场景，'
            '但不代表整座地牢，也不默认等同于其他探险的落点。'
        )
    return (
        '【本次局部探索域（后端冻结）】\n'
        f'目标地牢「{dungeon}」是可供多支队伍并发探索的宏观区域与主题身份，不是单一房间或唯一路线。\n'
        f'{anchor}\n'
        '同地牢其它地点、局部情报和其它队伍所在处只有在输入已建立移动、传播或共同主体因果时才可进入本场；'
        '不得为展示整座地牢而强行串场。'
    )


def prepare_locus_location_snapshot(
    expedition_log_id: int | None,
    target_dungeon: str,
    location_snapshot: dict,
    *,
    preserve_snapshot: bool = False,
) -> tuple[dict, dict]:
    """Soft auxiliary boundary: invalid/missing locus preserves the legacy snapshot."""
    if not expedition_log_id:
        return {}, dict(location_snapshot or {})
    try:
        locus = load_expedition_locus_snapshot(expedition_log_id, target_dungeon)
        if not locus:
            return {}, dict(location_snapshot or {})
        if preserve_snapshot:
            from plugins.adventurer_s1.services.s1_world_location_state_service import (
                S1WorldLocationStateService,
            )
            projected = dict(location_snapshot)
            projected['prompt_block'] = S1WorldLocationStateService.render_prompt_block(
                projected, locus_prompt_block=_render_locus_prompt_block(locus),
            )
            return locus, projected
        return locus, project_location_snapshot_to_locus(location_snapshot, locus)
    except Exception as exc:  # noqa: BLE001 - locality must never trigger an AI retry
        try:
            from flask import current_app

            current_app.logger.warning(
                'S1 局部探索域投影失败，已降级为地牢级上下文 log=%s dungeon=%s error=%s',
                expedition_log_id,
                target_dungeon,
                str(exc)[:300],
            )
        except Exception:
            pass
        return {}, dict(location_snapshot or {})


def prepare_expedition_location_snapshot(
    expedition_log_id: int | None, target_dungeon: str,
) -> tuple[dict, dict]:
    """Reuse the same authority across outline, narrative and ordinary recovery."""
    from .adventurer_s1_config_service import is_s1_independent_dungeon
    if is_s1_independent_dungeon(target_dungeon):
        return {}, {
            'target_dungeon': target_dungeon, 'locations': [],
            'is_explorable': True, 'prompt_block': '',
        }
    from plugins.adventurer_s1.services.s1_world_location_state_service import (
        S1WorldLocationStateService,
    )
    snapshot = (
        S1WorldLocationStateService.load_snapshot(expedition_log_id)
        if expedition_log_id else {}
    )
    frozen = bool(
        isinstance(snapshot, dict)
        and str(snapshot.get('target_dungeon') or '').strip() == str(target_dungeon or '').strip()
        and isinstance(snapshot.get('locations'), list)
    )
    if not frozen:
        snapshot = S1WorldLocationStateService.build_snapshot(target_dungeon)
    locus, snapshot = prepare_locus_location_snapshot(
        expedition_log_id, target_dungeon, snapshot, preserve_snapshot=frozen,
    )
    if not snapshot.get('prompt_block'):
        snapshot['prompt_block'] = S1WorldLocationStateService.render_prompt_block(snapshot)
    if not frozen:
        S1WorldLocationStateService.persist_snapshot(expedition_log_id, snapshot)
    return locus, snapshot


def expedition_locus_scope_key_from_full_log(
    full_log: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> str | None:
    """Return the frozen per-run scope key, including a transient frontier key."""
    if not isinstance(full_log, dict):
        return None
    locus = normalize_expedition_locus_snapshot(
        full_log.get(EXPEDITION_LOCUS_LOG_FIELD),
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    return str(locus.get('scope_key') or '').strip() or None


def resolve_expedition_locus_scope_from_full_log(
    full_log: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> tuple[str, str | None]:
    """Distinguish a true legacy log from a present-but-invalid frozen locus."""
    if not isinstance(full_log, dict):
        return 'legacy', None
    if EXPEDITION_LOCUS_LOG_FIELD not in full_log:
        return 'legacy', None
    locus = normalize_expedition_locus_snapshot(
        full_log.get(EXPEDITION_LOCUS_LOG_FIELD),
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if not locus:
        return 'unresolved', None
    return 'valid', str(locus.get('scope_key') or '').strip() or None


def resolve_persistent_story_scope_from_full_log(
    full_log: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> tuple[str, str | None]:
    """Classify a settlement source as legacy, stable-local, or unresolved-local.

    A valid frontier without exactly one applied location is deliberately
    unresolved: maintenance may omit its local world-story write, but must not
    widen that fact to the whole dungeon.
    """
    if not isinstance(full_log, dict):
        return 'legacy', None
    if EXPEDITION_LOCUS_LOG_FIELD not in full_log:
        return 'legacy', None
    raw_locus = full_log.get(EXPEDITION_LOCUS_LOG_FIELD)
    locus = normalize_expedition_locus_snapshot(
        raw_locus,
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if not locus:
        return 'unresolved', None
    stable_scope_key = persistent_location_scope_key_from_full_log(
        full_log,
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if stable_scope_key:
        return 'stable', stable_scope_key
    return 'unresolved', None


def persistent_location_scope_key_from_full_log(
    full_log: object,
    *,
    expected_target_dungeon: str | None = None,
    expected_expedition_log_id: int | None = None,
) -> str | None:
    """Resolve the stable local scope available to post-settlement maintenance."""
    if not isinstance(full_log, dict):
        return None
    locus = normalize_expedition_locus_snapshot(
        full_log.get(EXPEDITION_LOCUS_LOG_FIELD),
        expected_target_dungeon=expected_target_dungeon,
        expected_expedition_log_id=expected_expedition_log_id,
    )
    if not locus:
        return None
    if locus['kind'] == EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
        return locus['scope_key']
    applied_ids = {
        _coerce_positive_int(item.get('location_id'))
        for item in (full_log.get('location_state_application') or [])
        if isinstance(item, dict)
    }
    applied_ids.discard(None)
    if len(applied_ids) == 1:
        return location_scope_key(next(iter(applied_ids)))
    return None
