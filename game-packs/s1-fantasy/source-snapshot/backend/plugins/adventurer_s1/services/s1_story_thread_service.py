"""S1 隐藏剧情线：玩家不可见的长期伏笔记忆与 prompt 注入。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1StoryThread

logger = logging.getLogger(__name__)


ACTIVE_THREAD_STATUSES = ('active', 'dormant')
PROMPT_THREAD_STATUSES = ('active',)


def _story_thread_head_clause():
    return or_(
        AdventurerS1StoryThread.is_chain_head.is_(True),
        AdventurerS1StoryThread.is_chain_head.is_(None),
    )


THREAD_OUTPUT_STATUSES = {'active', 'dormant', 'resolved', 'deleted'}
STORY_THREAD_ENTITY_REF_KEYS = (
    'world_actor_ids',
    'world_story_thread_ids',
    'faction_keys',
)
STORY_THREAD_ENTITY_REF_LIMIT = 4


def _config_int(key: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(current_app.config.get(key, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _summary_max_len() -> int:
    return _config_int('ADVENTURER_S1_STORY_THREADS_SUMMARY_MAX_CHARS', 240, minimum=80)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_positive_int_ref(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else 0
    return 0


def _clean_text(value: Any, *, max_len: int) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) > max_len:
        return text[:max_len].rstrip()
    return text


def _normalize_status(value: Any) -> str:
    status = str(value or 'active').strip().lower()
    return status if status in THREAD_OUTPUT_STATUSES else 'active'


def _normalize_weight(value: Any) -> int:
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return 50
    return max(1, min(100, weight))


def _normalize_tags(value: Any) -> list[str]:
    raw_items: list[Any]
    if value is None:
        raw_items = []
    elif isinstance(value, str):
        raw_items = [p.strip() for p in value.replace('，', ',').split(',')]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []

    tags: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        tag = _clean_text(item, max_len=24)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= 8:
            break
    return tags


def normalize_story_thread_entity_refs(value: Any) -> dict[str, list]:
    """规范化个人剧情线的精确实体引用；不从标题、摘要或名称推断。"""
    if not isinstance(value, dict):
        return {}

    result: dict[str, list] = {}
    for key in ('world_actor_ids', 'world_story_thread_ids'):
        raw_values = value.get(key)
        if not isinstance(raw_values, list):
            continue
        normalized: list[int] = []
        seen: set[int] = set()
        for raw in raw_values:
            item = _normalize_positive_int_ref(raw)
            if item <= 0 or item in seen:
                continue
            seen.add(item)
            normalized.append(item)
            if len(normalized) >= STORY_THREAD_ENTITY_REF_LIMIT:
                break
        if normalized:
            result[key] = normalized

    raw_faction_keys = value.get('faction_keys')
    if isinstance(raw_faction_keys, list):
        faction_keys: list[str] = []
        seen_faction_keys: set[str] = set()
        for raw in raw_faction_keys:
            if not isinstance(raw, str):
                continue
            faction_key = _clean_text(raw, max_len=120)
            if not faction_key or faction_key in seen_faction_keys:
                continue
            seen_faction_keys.add(faction_key)
            faction_keys.append(faction_key)
            if len(faction_keys) >= STORY_THREAD_ENTITY_REF_LIMIT:
                break
        if faction_keys:
            result['faction_keys'] = faction_keys
    return result


def _validate_admin_entity_refs(value: Any) -> tuple[dict[str, list] | None, str | None]:
    """管理员写入也只接受数据库中已存在的稳定引用。"""
    from plugins.adventurer_s1.models import (
        AdventurerS1WorldActor,
        AdventurerS1WorldActorFactionRelation,
        AdventurerS1WorldStoryThread,
    )

    refs = normalize_story_thread_entity_refs(value)
    actor_ids = set(refs.get('world_actor_ids') or [])
    world_thread_ids = set(refs.get('world_story_thread_ids') or [])
    faction_keys = set(refs.get('faction_keys') or [])
    existing_actor_ids = {
        int(row[0]) for row in AdventurerS1WorldActor.query.with_entities(AdventurerS1WorldActor.id).filter(
            AdventurerS1WorldActor.id.in_(actor_ids),
        ).all()
    } if actor_ids else set()
    existing_world_thread_ids = {
        int(row[0]) for row in AdventurerS1WorldStoryThread.query.with_entities(
            AdventurerS1WorldStoryThread.id,
        ).filter(AdventurerS1WorldStoryThread.id.in_(world_thread_ids)).all()
    } if world_thread_ids else set()
    existing_faction_keys = {
        str(row[0]) for row in AdventurerS1WorldActorFactionRelation.query.with_entities(
            AdventurerS1WorldActorFactionRelation.faction_key,
        ).filter(AdventurerS1WorldActorFactionRelation.faction_key.in_(faction_keys)).distinct().all()
    } if faction_keys else set()
    missing = []
    missing.extend(f'world_actor_id={item}' for item in sorted(actor_ids - existing_actor_ids))
    missing.extend(
        f'world_story_thread_id={item}' for item in sorted(world_thread_ids - existing_world_thread_ids)
    )
    missing.extend(f'faction_key={item}' for item in sorted(faction_keys - existing_faction_keys))
    if missing:
        return None, f"实体引用不存在：{', '.join(missing)}"
    return refs, None


def _normalize_thread_payloads(value: Any) -> list[dict]:
    if not value:
        return []
    raw_items = [value] if isinstance(value, dict) else value
    if not isinstance(raw_items, list):
        return []

    payloads: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = _clean_text(raw.get('title') or raw.get('name'), max_len=80)
        summary = _clean_text(
            raw.get('summary') or raw.get('desc') or raw.get('description'),
            max_len=_summary_max_len(),
        )
        status = _normalize_status(raw.get('status'))
        if not title:
            continue
        if status != 'deleted' and not summary:
            continue
        payloads.append({
            'id': _coerce_int(raw.get('id') or raw.get('thread_id'), 0),
            'predecessor_id': _coerce_int(raw.get('predecessor_id'), 0),
            'title': title,
            'summary': summary,
            'tags': _normalize_tags(raw.get('tags')),
            'entity_refs': normalize_story_thread_entity_refs(raw.get('entity_refs')),
            'status': status,
            'weight': _normalize_weight(raw.get('weight')),
        })
        if len(payloads) >= 3:
            break
    return payloads


def _merge_entity_refs(*values: Any) -> dict[str, list]:
    merged: dict[str, list] = {}
    for key in STORY_THREAD_ENTITY_REF_KEYS:
        items: list = []
        seen: set = set()
        for value in values:
            for item in normalize_story_thread_entity_refs(value).get(key) or []:
                if item in seen:
                    continue
                seen.add(item)
                items.append(item)
                if len(items) >= STORY_THREAD_ENTITY_REF_LIMIT:
                    break
            if len(items) >= STORY_THREAD_ENTITY_REF_LIMIT:
                break
        if items:
            merged[key] = items
    return merged


def _allowed_entity_refs_from_generation_snapshot(full_log: dict) -> dict[str, set]:
    snapshot = full_log.get('generation_world_intel_injection')
    if not isinstance(snapshot, dict):
        return {key: set() for key in STORY_THREAD_ENTITY_REF_KEYS}
    actor_values = snapshot.get('world_actor_ids')
    actor_values = actor_values if isinstance(actor_values, list) else []
    item_values = snapshot.get('items')
    item_values = item_values if isinstance(item_values, list) else []
    faction_values = snapshot.get('faction_keys')
    faction_values = faction_values if isinstance(faction_values, list) else []
    return {
        'world_actor_ids': {
            item for item in (
                _normalize_positive_int_ref(value) for value in actor_values
            )
            if item > 0
        },
        'world_story_thread_ids': {
            item for item in (
                _normalize_positive_int_ref(row.get('id'))
                for row in item_values
                if isinstance(row, dict)
            )
            if item > 0
        },
        'faction_keys': {
            _clean_text(value, max_len=120)
            for value in faction_values
            if isinstance(value, str) and _clean_text(value, max_len=120)
        },
    }


def _validated_new_entity_refs(requested: Any, allowed: dict[str, set]) -> dict[str, list]:
    normalized = normalize_story_thread_entity_refs(requested)
    return {
        key: [item for item in normalized.get(key) or [] if item in allowed.get(key, set())]
        for key in STORY_THREAD_ENTITY_REF_KEYS
        if any(item in allowed.get(key, set()) for item in normalized.get(key) or [])
    }


def _build_entity_ref_prompt_labels(threads: list[AdventurerS1StoryThread]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for thread in threads:
        refs = normalize_story_thread_entity_refs(thread.entity_refs)
        if refs:
            labels[int(thread.id)] = (
                '[精确关系对象 entity_refs:'
                + json.dumps(refs, ensure_ascii=False, separators=(',', ':'))
                + ']'
            )
    return labels


def _truncate_prompt_line(text: str, max_len: int) -> str:
    cleaned = ' '.join(str(text or '').split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max(0, max_len - 1)].rstrip() + '…'


def _prune_story_threads_for_adventurer(adventurer_id: int) -> None:
    """个人剧情链使用软上限；历史与未收束 head 不做自动物理删除。"""
    max_active = _config_int('ADVENTURER_S1_STORY_THREADS_MAX_PER_ADVENTURER', 6)
    max_total = _config_int('ADVENTURER_S1_STORY_THREADS_MAX_TOTAL_PER_ADVENTURER', 10)
    head_query = AdventurerS1StoryThread.query.filter(
        AdventurerS1StoryThread.adventurer_id == adventurer_id,
        _story_thread_head_clause(),
    )
    active_count = head_query.filter(AdventurerS1StoryThread.status.in_(ACTIVE_THREAD_STATUSES)).count()
    total_count = head_query.count()
    if active_count > max_active or total_count > max_total:
        logger.warning(
            'S1 个人剧情链 head 超过软上限 adv=%s active=%s/%s total=%s/%s；保留未收束线等待自然收束',
            adventurer_id, active_count, max_active, total_count, max_total,
        )


def _thread_relevance_score(thread: AdventurerS1StoryThread, target_dungeon: str) -> int:
    """只按结构化地牢标签与生命周期排序，不扫描成员或剧情自然语言。"""
    target = (target_dungeon or '').strip()
    tags = [str(t).strip() for t in (thread.tags or []) if str(t).strip()]
    weight = max(1, min(100, int(thread.weight or 50)))
    if target and target in tags:
        score = 70
    elif thread.status == 'active':
        score = 1
    else:
        return 0
    if thread.status == 'active':
        score += 10
    return score + weight


def _shared_explicit_tags(threads: list[AdventurerS1StoryThread]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for thread in threads:
        for tag in {str(t).strip() for t in (thread.tags or []) if str(t).strip()}:
            counts[tag] += 1
    return [tag for tag, count in counts.items() if count >= 2][:6]


def build_s1_story_threads_prompt_block(
    members,
    target_dungeon: str,
    *,
    name_alias: dict[int, str] | None = None,
) -> str:
    """按相关性构建隐藏剧情线 prompt 块；无相关线索时返回空字符串。"""
    from .adventurer_s1_config_service import is_s1_independent_dungeon
    if is_s1_independent_dungeon(target_dungeon):
        return ''
    member_ids = [int(m.id) for m in members or [] if getattr(m, 'id', None) is not None]
    if not member_ids:
        return ''

    per_member_limit = _config_int('ADVENTURER_S1_STORY_THREADS_PER_MEMBER', 2)
    total_limit = _config_int('ADVENTURER_S1_STORY_THREADS_PROMPT_LIMIT', 8)
    char_budget = _config_int('ADVENTURER_S1_STORY_THREADS_PROMPT_CHAR_BUDGET', 1200, minimum=200)
    line_max = _config_int('ADVENTURER_S1_STORY_THREADS_PROMPT_LINE_MAX_CHARS', 180, minimum=80)
    threads = (
        AdventurerS1StoryThread.query
        .filter(
            AdventurerS1StoryThread.adventurer_id.in_(member_ids),
            _story_thread_head_clause(),
            AdventurerS1StoryThread.status.in_(ACTIVE_THREAD_STATUSES),
        )
        .order_by(
            AdventurerS1StoryThread.adventurer_id,
            AdventurerS1StoryThread.weight.desc(),
            AdventurerS1StoryThread.last_touched_at.desc(),
        )
        .all()
    )

    active_threads = [thread for thread in threads if thread.status in PROMPT_THREAD_STATUSES]
    dormant_threads = [thread for thread in threads if thread.status == 'dormant']
    scored: list[tuple[int, AdventurerS1StoryThread]] = []
    for thread in active_threads:
        score = _thread_relevance_score(
            thread,
            target_dungeon,
        )
        if score > 0:
            scored.append((score, thread))
    fallback_pick = False
    if not scored:
        # 地牢文字未匹配时仍提供一条可选旧线，避免长期线索永久沉底；候选不承担补内容职责。
        # 是否实际触发由生成 AI 结合完整客观局势判断；这里只做年龄明确的机械候选选择。
        fallback_thread = min(
            active_threads,
            key=lambda thread: (
                thread.last_touched_at,
                int(thread.weight or 0),
                thread.id,
            ),
            default=None,
        )
        if fallback_thread is not None:
            scored = [(0, fallback_thread)]
            fallback_pick = True
    else:
        scored.sort(key=lambda item: (item[0], item[1].last_touched_at), reverse=True)
    picked: list[AdventurerS1StoryThread] = []
    picked_count_by_member: dict[int, int] = {}
    for _, thread in scored:
        adv_id = int(thread.adventurer_id)
        if picked_count_by_member.get(adv_id, 0) >= per_member_limit:
            continue
        picked.append(thread)
        picked_count_by_member[adv_id] = picked_count_by_member.get(adv_id, 0) + 1
        if len(picked) >= total_limit:
            break
    closure_ids: set[int] = set()
    stale_days = _config_int('ADVENTURER_S1_STORY_THREAD_CLOSURE_STALE_DAYS', 7, minimum=1)
    cutoff = _portable_utc_now() - timedelta(days=stale_days)
    closure_thread = min(
        (thread for thread in dormant_threads if thread.last_touched_at <= cutoff),
        key=lambda thread: (thread.last_touched_at, int(thread.weight or 0), thread.id),
        default=None,
    )
    if closure_thread and closure_thread not in picked:
        if len(picked) >= total_limit and picked:
            picked.pop()
        # 收束候选独立保留一个名额，并放在前面避免被字符预算截掉。
        picked.insert(0, closure_thread)
        closure_ids.add(int(closure_thread.id))
    if not picked:
        return ''

    header = (
        '【隐藏剧情线（玩家不可见，仅供延续长期伏笔；不要求本次触发）】\n'
        '· 本块只是候选记忆，不是本次主线指令；只有与当前地牢、委托目标和现场局势自然吻合时才可使用。\n'
        '· 本次最多推进或收束 1 个自然吻合的个人伏笔主题簇；多名成员的已注入线索共享显式标签时，'
        '实际采用与阶段结果仍由各自的既有联系、知识及本场交互成立，并分别写回各自的 story_threads。\n'
        '· 每场只强制裁决本次副本目标，不强制处理个人线；个人线若在正文承担独立场景、关键转折、目标变化或主要结尾功能，'
        '必须形成改变后续继承事实的阶段结果。只重复旧信息或加强未来可能性不算推进，应保留原线不更新。\n'
        '· 优先把已有线索完成、降为 dormant 或 resolved；不要把已解决事件继续改写成更大阴谋的下一节点，'
        '也不要为了个人线改变本次副本目标、地牢生态或公共世界线。个人线的触发/收束默认不是 world_fact_updates。\n'
        '· 每条前缀 [成员id]名字 是该剧情线的唯一归属成员：只能延续到该成员本人，'
        '更新/收束也只能写进该成员的 member_update.story_threads；'
        '不要把某成员的剧情线改写成由他人承担，也不要据此为别的成员新建同主题剧情线。'
        '\n· 每条中的“精确关系对象”仅锁定世界角色、公共世界线或势力的身份，不表示本次必须触发、对象必须出场、关系方向或处置权限；'
        '输出 entity_refs 时只能原样保留这些稳定 ID/key，或使用本次世界情报块明确展示的 ID/key，禁止用 M 编号、名字或文本猜测身份。'
        '\n· 标为“个人收束候选”的 dormant 旧节点只在本场因果支持结案时采用；没有真实变化时保留原状态。'
        '同一阶段用线 id 原行更新；只有旧阶段已有明确结果并自然产生独立新阶段时，才用 predecessor_id 新建后继。'
    )
    if fallback_pick:
        header += (
            '\n· 下方是“可选长期伏笔”：文字未匹配当前地牢，只供核对是否存在自然交集。'
            '采用条件与阶段结果按 system 的【深渊酒馆·隐藏剧情线（玩家不可见）】执行；没有实际交集时保持未采用。'
        )
    if len(header) > max(0, char_budget - line_max):
        fallback_line = (
            '\n· 可选长期伏笔：仅在既有联系与本场交互自然相交时采用，没有实际交集时保持未采用。'
            if fallback_pick else ''
        )
        header = (
            '【隐藏剧情线（玩家不可见）】\n'
            '· 候选记忆，不是主线指令；自然吻合时才轻触发。\n'
            '· 最多推进/收束 1 个个人伏笔主题簇；共享显式标签者可一起轻收束。\n'
            '· 只写回各自 story_threads，默认不是 world_fact_updates，不改变副本目标或公共世界线。'
            '\n· 精确关系对象只锁定身份，不强制触发或授权；entity_refs 只能保留已显示的稳定 ID/key，禁止用 M 编号、名字或文本猜测。'
            f'{fallback_line}'
        )
    lines = [header]
    used_chars = len(header)
    entity_ref_labels = _build_entity_ref_prompt_labels(picked)
    shared_tags = _shared_explicit_tags(picked)
    if shared_tags:
        cluster_line = f"· 已注入线索存在共享显式标签：{'、'.join(shared_tags)}；这只表示可作为同一主题簇轻触发/收束，不表示公共世界事实。"
        projected = used_chars + len(cluster_line) + 1
        if projected <= char_budget:
            lines.append(cluster_line)
            used_chars = projected
    for thread in picked:
        member_name = (name_alias or {}).get(int(thread.adventurer_id)) or f'#{thread.adventurer_id}'
        entity_ref_label = entity_ref_labels.get(int(thread.id), '')
        line_prefix = (
            f"- [{thread.adventurer_id}]{member_name}："
            f"{'[个人收束候选] ' if int(thread.id) in closure_ids else ''}"
            f"[线id:{thread.id}] "
        )
        remaining_content = char_budget - used_chars - 1 - len(line_prefix)
        entity_ref_chars = len(entity_ref_label) + (1 if entity_ref_label else 0)
        if remaining_content <= entity_ref_chars:
            continue
        narrative_max = min(
            max(40, line_max - entity_ref_chars),
            remaining_content - entity_ref_chars,
        )
        prompt_content = ' '.join(part for part in (
            entity_ref_label,
            _truncate_prompt_line(thread.to_prompt_line(), narrative_max),
        ) if part)
        line = f"{line_prefix}{prompt_content}"
        projected = used_chars + len(line) + 1
        lines.append(line)
        used_chars = projected
    if len(lines) == 1:
        return ''
    return '\n'.join(lines)


def apply_s1_story_thread_updates(members, full_log: dict, *, source_log_id: int | None = None) -> None:
    """根据 member_updates[*].story_threads 新增/修改/停用隐藏剧情线。须在事务内调用。"""
    if not isinstance(full_log, dict):
        return
    from .adventurer_s1_config_service import is_s1_independent_dungeon
    if is_s1_independent_dungeon(full_log.get('target_dungeon')):
        return
    member_ids = {int(m.id) for m in members or [] if getattr(m, 'id', None) is not None}
    if not member_ids:
        return

    now = _portable_utc_now()
    existing = (
        AdventurerS1StoryThread.query
        .filter(AdventurerS1StoryThread.adventurer_id.in_(member_ids))
        .all()
    )
    by_key = {
        (int(thread.adventurer_id), thread.title): thread
        for thread in existing
        if thread.is_chain_head is not False
    }
    by_id = {int(thread.id): thread for thread in existing}
    all_by_key = {
        (int(thread.adventurer_id), thread.title): thread
        for thread in existing
    }
    # 注入前各 title 的归属成员（只看 DB 已有，不含本次新建），用于拦截 AI 把他人旧剧情线搬运到别人名下
    existing_title_owners: dict[str, set[int]] = defaultdict(set)
    for thread in existing:
        existing_title_owners[thread.title].add(int(thread.adventurer_id))

    touched_member_ids: set[int] = set()
    allowed_new_refs = _allowed_entity_refs_from_generation_snapshot(full_log)

    for update in full_log.get('member_updates') or []:
        if not isinstance(update, dict) or update.get('id') is None:
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        if adv_id not in member_ids:
            continue

        for payload in _normalize_thread_payloads(update.get('story_threads')):
            key = (adv_id, payload['title'])
            requested_id = int(payload.get('id') or 0)
            predecessor_id = int(payload.get('predecessor_id') or 0)
            thread = by_id.get(requested_id) if requested_id > 0 else by_key.get(key)
            is_new_thread = thread is None
            if thread is not None and (
                int(thread.adventurer_id) != adv_id
                or thread.is_chain_head is False
            ):
                logger.warning('S1 剧情线拒绝更新非本人或非 head 节点 adv=%s thread=%s', adv_id, requested_id)
                continue
            if requested_id > 0 and thread is None:
                logger.warning('S1 剧情线拒绝更新不存在节点 adv=%s thread=%s', adv_id, requested_id)
                continue
            if payload['status'] == 'deleted':
                if thread is not None:
                    db_manager.delete_instance(thread, flush=False)
                    by_key.pop(key, None)
                continue

            if thread is None:
                # 归属保护：若该 title 注入前已属本队其他成员，说明 AI 把他人旧剧情线搬到了 adv_id 名下，拒绝新建以免污染长期记忆
                other_owners = existing_title_owners.get(payload['title'], set()) - {adv_id}
                if other_owners:
                    logger.warning(
                        'S1 剧情线归属拦截：拒绝把【%s】新建到 adv=%s（注入前已属本队成员 %s）',
                        payload['title'], adv_id, sorted(other_owners),
                    )
                    continue
                if key in all_by_key:
                    logger.warning('S1 剧情线拒绝复用历史节点标题 adv=%s title=%s', adv_id, payload['title'])
                    continue
                predecessor = by_id.get(predecessor_id) if predecessor_id > 0 else None
                if predecessor_id > 0 and (
                    predecessor is None
                    or int(predecessor.adventurer_id) != adv_id
                    or predecessor.is_chain_head is False
                    or predecessor.superseded_by_id
                    or predecessor.title == payload['title']
                    or predecessor.status not in ('dormant', 'resolved')
                ):
                    logger.warning('S1 剧情线拒绝无效后继关系 adv=%s predecessor=%s', adv_id, predecessor_id)
                    continue
                thread = AdventurerS1StoryThread(
                    adventurer_id=adv_id,
                    title=payload['title'],
                )
                by_key[key] = thread
                all_by_key[key] = thread
                db_manager.add(thread, flush=False)
                if predecessor:
                    ok, err = db_manager.flush()
                    if not ok:
                        raise RuntimeError(err or '个人剧情线后继节点 flush 失败')
                    thread.chain_key = predecessor.chain_key or f'personal-story:{adv_id}:{int(predecessor.id)}'
                    thread.predecessor_id = int(predecessor.id)
                    thread.is_chain_head = True
                    predecessor.is_chain_head = False
                    predecessor.superseded_by_id = int(thread.id)
                    thread.entity_refs = normalize_story_thread_entity_refs(predecessor.entity_refs)
                ok, err = db_manager.flush()
                if not ok:
                    raise RuntimeError(err or '个人剧情线节点 flush 失败')
                by_id[int(thread.id)] = thread

            validated_additions = _validated_new_entity_refs(payload['entity_refs'], allowed_new_refs)
            # 已有引用是身份锚点：AI 省略、传空或传入未注入对象时均不能删除/改绑。
            thread.entity_refs = _merge_entity_refs(thread.entity_refs, validated_additions)

            old_status = getattr(thread, 'status', None)
            old_weight = int(getattr(thread, 'weight', 50) or 50)
            thread.summary = _clean_text(payload['summary'], max_len=_summary_max_len())
            thread.tags = payload['tags']
            thread.status = payload['status']
            next_weight = payload['weight']
            if not is_new_thread and old_status == 'active' and payload['status'] == 'active':
                # 反复保持 active 的旧线索自动降权，避免同一伏笔永久高权重注入。
                next_weight = min(next_weight, max(1, old_weight - 15))
            thread.weight = next_weight
            thread.source_log_id = source_log_id
            thread.last_touched_at = now
            thread.updated_at = now
            touched_member_ids.add(adv_id)

    for adv_id in touched_member_ids:
        _prune_story_threads_for_adventurer(adv_id)


def list_admin_story_threads(adventurer_id: int) -> tuple[list[dict] | None, str | None]:
    """管理员查看冒险者全部隐藏剧情线（含 resolved）。玩家接口禁止调用。"""
    from plugins.adventurer_s1.models import AdventurerS1

    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
    if not adv:
        return None, '冒险者不存在'

    threads = (
        AdventurerS1StoryThread.query
        .filter_by(adventurer_id=adventurer_id)
        .all()
    )
    status_order = {'active': 0, 'dormant': 1, 'resolved': 2}
    threads.sort(key=lambda t: (
        status_order.get(t.status, 9),
        -int(t.weight or 0),
        -(t.last_touched_at.timestamp() if t.last_touched_at else 0),
    ))
    return [thread.to_admin_dict() for thread in threads], None


ADMIN_THREAD_STATUSES = ('active', 'dormant', 'resolved')


def _thread_to_admin_list_dict(thread: AdventurerS1StoryThread, adv, user) -> dict:
    data = thread.to_admin_dict()
    data['adventurer_name'] = getattr(adv, 'name', None)
    data['adventurer_status'] = getattr(adv, 'status', None)
    data['owner_id'] = getattr(adv, 'owner_id', None)
    data['username'] = getattr(user, 'username', None) if user else None
    data['nickname'] = getattr(user, 'nickname', None) if user else None
    return data


def get_admin_story_thread_stats() -> dict:
    """全服隐藏剧情线概览统计（管理员专用）。"""
    from sqlalchemy import func

    from plugins.adventurer_s1.models import AdventurerS1

    total_threads = int(
        AdventurerS1StoryThread.query.with_entities(func.count(AdventurerS1StoryThread.id)).scalar() or 0
    )
    adventurers_with_threads = int(
        AdventurerS1StoryThread.query.with_entities(
            func.count(func.distinct(AdventurerS1StoryThread.adventurer_id)),
        ).scalar() or 0
    )
    alive_adventurers = int(
        AdventurerS1.query.filter(AdventurerS1.status != 'archived').count()
    )
    status_rows = (
        AdventurerS1StoryThread.query.with_entities(
            AdventurerS1StoryThread.status,
            func.count(AdventurerS1StoryThread.id),
        )
        .group_by(AdventurerS1StoryThread.status)
        .all()
    )
    by_status = {status: 0 for status in ADMIN_THREAD_STATUSES}
    for status, count in status_rows:
        key = str(status or '').strip().lower()
        if key in by_status:
            by_status[key] = int(count or 0)

    avg_per_holder = round(total_threads / adventurers_with_threads, 2) if adventurers_with_threads else 0.0
    coverage_pct = round(adventurers_with_threads / alive_adventurers * 100, 1) if alive_adventurers else 0.0

    return {
        'total_threads': total_threads,
        'adventurers_with_threads': adventurers_with_threads,
        'alive_adventurers': alive_adventurers,
        'coverage_pct': coverage_pct,
        'avg_threads_per_holder': avg_per_holder,
        'by_status': by_status,
    }


def search_admin_story_threads(
    *,
    search: str = '',
    status: str = '',
    adventurer_id: int | None = None,
    page: int = 1,
    per_page: int = 30,
) -> tuple[dict | None, str | None]:
    """管理员分页搜索全服隐藏剧情线。"""
    from sqlalchemy import cast, or_, String

    from models.user import User
    from plugins.adventurer_s1.models import AdventurerS1

    query = (
        AdventurerS1StoryThread.query
        .join(AdventurerS1, AdventurerS1StoryThread.adventurer_id == AdventurerS1.id)
        .outerjoin(User, AdventurerS1.owner_id == User.id)
    )

    keyword = ' '.join(str(search or '').split())
    if keyword:
        if len(keyword) < 2:
            return None, '搜索词至少 2 个字符'
        like = f'%{keyword}%'
        query = query.filter(or_(
            AdventurerS1StoryThread.title.ilike(like),
            AdventurerS1StoryThread.summary.ilike(like),
            cast(AdventurerS1StoryThread.tags, String).ilike(like),
            AdventurerS1.name.ilike(like),
            User.username.ilike(like),
            User.nickname.ilike(like),
        ))

    status_filter = str(status or '').strip().lower()
    if status_filter:
        if status_filter not in ADMIN_THREAD_STATUSES:
            return None, 'status 仅支持 active、dormant、resolved'
        query = query.filter(AdventurerS1StoryThread.status == status_filter)

    if adventurer_id is not None:
        try:
            adv_id = int(adventurer_id)
        except (TypeError, ValueError):
            return None, 'adventurer_id 无效'
        query = query.filter(AdventurerS1StoryThread.adventurer_id == adv_id)

    total = query.count()
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 30), 100))
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    rows = (
        query
        .order_by(
            AdventurerS1StoryThread.last_touched_at.desc(),
            AdventurerS1StoryThread.id.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    adv_ids = {thread.adventurer_id for thread in rows}
    adv_map = {
        adv.id: adv
        for adv in AdventurerS1.query.filter(AdventurerS1.id.in_(adv_ids)).all()
    } if adv_ids else {}
    owner_ids = {adv.owner_id for adv in adv_map.values() if adv and adv.owner_id}
    user_map = {
        user.id: user
        for user in User.query.filter(User.id.in_(owner_ids)).all()
    } if owner_ids else {}

    items = []
    for thread in rows:
        adv = adv_map.get(thread.adventurer_id)
        user = user_map.get(adv.owner_id) if adv and adv.owner_id else None
        items.append(_thread_to_admin_list_dict(thread, adv, user))

    from sqlalchemy import func as sa_func
    filtered_adventurers = int(
        query.with_entities(sa_func.count(sa_func.distinct(AdventurerS1StoryThread.adventurer_id))).scalar() or 0
    ) if total else 0

    return {
        'search': keyword,
        'status': status_filter or None,
        'adventurer_id': int(adventurer_id) if adventurer_id is not None else None,
        'summary': {
            'total_threads': total,
            'unique_adventurers': filtered_adventurers,
        },
        'items': items,
        'page': page,
        'pages': pages,
        'total': total,
        'per_page': per_page,
    }, None


def create_admin_story_thread(payload: dict) -> tuple[dict | None, str | None]:
    """管理员手动新增隐藏剧情线。"""
    from plugins.adventurer_s1.models import AdventurerS1

    body = payload or {}
    try:
        adv_id = int(body.get('adventurer_id'))
    except (TypeError, ValueError):
        return None, '请提供有效的 adventurer_id'

    adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
    if not adv:
        return None, '冒险者不存在'

    title = _clean_text(body.get('title'), max_len=80)
    summary = _clean_text(body.get('summary'), max_len=_summary_max_len())
    if not title:
        return None, '标题不能为空'
    if not summary:
        return None, '摘要不能为空'

    status = _normalize_status(body.get('status'))
    if status not in ADMIN_THREAD_STATUSES:
        status = 'active'

    existing = AdventurerS1StoryThread.query.filter_by(adventurer_id=adv_id, title=title).first()
    if existing:
        return None, f'该冒险者已有同名剧情线【{title}】'
    entity_refs, entity_refs_error = _validate_admin_entity_refs(body.get('entity_refs'))
    if entity_refs_error:
        return None, entity_refs_error

    now = _portable_utc_now()
    thread = AdventurerS1StoryThread(
        adventurer_id=adv_id,
        title=title,
        summary=summary,
        tags=_normalize_tags(body.get('tags')),
        entity_refs=entity_refs,
        status=status,
        weight=_normalize_weight(body.get('weight')),
        source_log_id=None,
        last_touched_at=now,
        created_at=now,
        updated_at=now,
    )

    with db_manager.transaction():
        db_manager.add(thread, flush=False)
        _prune_story_threads_for_adventurer(adv_id)

    return thread.to_admin_dict(), None


def update_admin_story_thread(thread_id: int, payload: dict) -> tuple[dict | None, str | None]:
    """管理员修改隐藏剧情线。"""
    thread = AdventurerS1StoryThread.query.session.get(AdventurerS1StoryThread, thread_id)
    if not thread:
        return None, '剧情线不存在'

    body = payload or {}
    new_title = _clean_text(body.get('title'), max_len=80) if 'title' in body else thread.title
    if not new_title:
        return None, '标题不能为空'

    if new_title != thread.title:
        dup = AdventurerS1StoryThread.query.filter_by(
            adventurer_id=thread.adventurer_id,
            title=new_title,
        ).filter(AdventurerS1StoryThread.id != thread.id).first()
        if dup:
            return None, f'该冒险者已有同名剧情线【{new_title}】'

    new_summary = None
    if 'summary' in body:
        new_summary = _clean_text(body.get('summary'), max_len=_summary_max_len())
        if not new_summary:
            return None, '摘要不能为空'

    new_status = None
    if 'status' in body:
        new_status = _normalize_status(body.get('status'))
        if new_status not in ADMIN_THREAD_STATUSES:
            return None, 'status 仅支持 active、dormant、resolved'
    new_entity_refs = None
    if 'entity_refs' in body:
        new_entity_refs, entity_refs_error = _validate_admin_entity_refs(body.get('entity_refs'))
        if entity_refs_error:
            return None, entity_refs_error

    now = _portable_utc_now()
    with db_manager.transaction():
        if 'title' in body:
            thread.title = new_title
        if new_summary is not None:
            thread.summary = new_summary
        if 'tags' in body:
            thread.tags = _normalize_tags(body.get('tags'))
        if 'entity_refs' in body:
            thread.entity_refs = new_entity_refs
        if new_status is not None:
            thread.status = new_status
        if 'weight' in body:
            thread.weight = _normalize_weight(body.get('weight'))
        thread.last_touched_at = now
        thread.updated_at = now
        _prune_story_threads_for_adventurer(thread.adventurer_id)

    return thread.to_admin_dict(), None


def delete_admin_story_thread(thread_id: int) -> tuple[dict | None, str | None]:
    """管理员删除隐藏剧情线。"""
    thread = AdventurerS1StoryThread.query.session.get(AdventurerS1StoryThread, thread_id)
    if not thread:
        return None, '剧情线不存在'

    title = thread.title
    adv_id = thread.adventurer_id
    with db_manager.transaction():
        db_manager.delete_instance(thread, flush=False)

    return {'message': f'已删除剧情线【{title}】', 'adventurer_id': adv_id}, None
