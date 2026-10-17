"""深渊酒馆 S1 特质/技能描述补全 AI 服务。

探险主 AI 偶尔返回的 new_traits / new_skills 缺少 desc 字段。本服务在探险结算后
由 AsyncTaskService worker 异步调用，为缺失描述的特质和技能生成简洁描述文本。

设计要点：
- 证据补全：仅处理本次新增条目，提供完整正文与成员结算证据。
- 失败兜底：AI 无有效返回时特质/技能保留现有描述（含静态兜底文案），不阻塞流程。
- 与 trait_recovery_ai 并行投递，互不依赖。
"""
import json
import logging

import requests

from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    AIProfileAssignmentError,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_ai_player_name_service import (
    post_s1_ai_request_with_player_aliases as post_s1_ai_request,
    with_player_name_aliases,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
)
from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Skill,
    AdventurerS1Trait,
    AdventurerS1ExpeditionLog,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from plugins.adventurer_s1.services.s1_ai_prompt_shared import (
    S1_DEATH_FINALITY_ASSET_RULES,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.services.s1_description_repair_contract import (
    is_missing_asset_description,
)

logger = logging.getLogger(__name__)


class _DeferredDescBackfillTokenUsage:
    """Delay one provider attempt until the worker finishes its DB consumer."""

    def __init__(
        self,
        *,
        usage: dict,
        model: str,
        expedition_log_id: int | None,
    ) -> None:
        self._usage = usage
        self._model = model
        self._expedition_log_id = expedition_log_id
        self._finalized = False

    def finalize(self, *, is_success: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        record_token_usage(
            call_type='desc_backfill_ai',
            usage=self._usage,
            model=self._model,
            expedition_log_id=self._expedition_log_id,
            attempt_number=1,
            is_success=is_success,
        )


class _DescBackfillAIResult(dict):
    """Keep the public mapping contract while carrying deferred usage state."""

    def __init__(
        self,
        value: dict[tuple[str, int], str],
        *,
        deferred_token_usage: _DeferredDescBackfillTokenUsage | None,
        contract_valid: bool,
    ) -> None:
        super().__init__(value)
        self.deferred_token_usage = deferred_token_usage
        self.contract_valid = contract_valid is True


_DESC_BACKFILL_SYSTEM_PROMPT = S1_DEATH_FINALITY_ASSET_RULES + "\n\n" + """你是深渊酒馆的“描述补全员”。给你一批冒险者在探险中新获得、但缺少描述的特质和技能，以及本次已经冻结的结算证据。你的唯一任务是把证据中已经成立的长期结果写成玩家可长期查看的数据描述。

【要求】
- 技能描述写完整当前效果：说明可复现的作用、适用对象或触发条件，并保留证据已经建立的代价、限制或当前状态；只写一次剧情动作或来历不构成技能描述。
- 特质描述写清稳定状态、持续影响或已经建立的恢复边界；来历只用于帮助解释当前效果。
- 历史显示占位只表示缺失描述，不是已确定的效果；只输出有证据的实际描述，不能用占位文案填充 results。
- 描述只采用共享 all_events 正文与对应条目 evidence 中已经成立的事实。证据不足以确定效果的条目不输出，让其保留待处理状态。
- 证据按【生命终态边界】只保留可独立成立的合法机制；没有合法机制可写时不输出该条目，不另创能力替代越权内容。
- 每条描述保持紧凑，但完整性优先于固定字数。
- 风格采用深渊酒馆的奇幻冒险语境。
- 用玩家可理解的世界内效果描述机制，不写后台字段或系统术语。
- 不要重复特质/技能名本身。

【输出格式】
只返回合法 JSON，按 id 一一对应：
{
  "results": [
    {"type": "<trait|skill>", "id": <正整数>, "desc": "<基于 evidence 的完整当前描述>"}
  ]
}
"""


def _build_desc_backfill_user_prompt(items: list[dict]) -> str:
    # 同一探险的完整正文只发送一次，避免每条新增资产重复整篇故事。
    all_events = next((item.get('evidence', {}).get('all_events') for item in items
                       if item.get('evidence', {}).get('all_events')), [])
    records = []
    for item in items:
        record = {key: item[key] for key in (
            'type', 'id', 'name', 'adventurer_name', 'dungeon_theme',
        ) if key in item}
        record['evidence'] = {key: value for key, value in item.get('evidence', {}).items()
                              if key != 'all_events'}
        records.append(record)
    return (
        "请结合共享 all_events 正文和每条记录的 evidence 生成描述，按 type+id 一一对应输出 results；"
        "证据不足的记录不输出。\n"
        "待补全记录：\n"
        + json.dumps({'all_events': all_events, 'items': records}, ensure_ascii=False)
    )


def _parse_desc_backfill_results_with_status(
    parsed: dict | None,
    *,
    allowed_keys: set[tuple[str, int]] | None = None,
) -> tuple[dict[tuple[str, int], str] | None, bool]:
    """解析补全结果，并区分可安全消费子集与完整输出契约状态。"""
    if not isinstance(parsed, dict):
        return None, False
    results = parsed.get('results')
    if isinstance(results, dict) and allowed_keys is not None and len(allowed_keys) == 1:
        only_key = next(iter(allowed_keys))
        singleton_id, _ = normalize_ascii_integer(results.get('id'), minimum=1)
        if (results.get('type'), singleton_id) == only_key:
            results = [results]
    if not isinstance(results, list):
        return None, False
    mapping: dict[tuple[str, int], str] = {}
    contract_valid = True
    for entry in results:
        if not isinstance(entry, dict):
            contract_valid = False
            continue
        item_type = entry.get('type')
        if item_type not in ('trait', 'skill'):
            contract_valid = False
            continue
        item_id, _ = normalize_ascii_integer(entry.get('id'), minimum=1)
        if type(item_id) is not int:
            contract_valid = False
            continue
        key = (item_type, item_id)
        if allowed_keys is not None and key not in allowed_keys:
            contract_valid = False
            continue
        if key in mapping:
            contract_valid = False
            continue
        raw_desc = entry.get('desc')
        if not isinstance(raw_desc, str):
            contract_valid = False
            continue
        desc = raw_desc.strip()
        if is_missing_asset_description(item_type, desc):
            contract_valid = False
            continue
        mapping[key] = desc
    if allowed_keys is not None and set(mapping) != allowed_keys:
        contract_valid = False
    return mapping or None, bool(mapping) and contract_valid


def _parse_desc_backfill_results(
    parsed: dict | None,
    *,
    allowed_keys: set[tuple[str, int]] | None = None,
) -> dict[tuple[str, int], str] | None:
    """把 AI 返回的 results 转成 {(type, id): desc} 映射。"""
    mapping, _ = _parse_desc_backfill_results_with_status(
        parsed,
        allowed_keys=allowed_keys,
    )
    return mapping


@with_player_name_aliases
def _call_desc_backfill_ai(
    items: list[dict],
    expedition_log_id: int | None = None,
) -> dict[tuple[str, int], str] | None:
    """调用描述补全 AI，返回 {(type, id): desc}；失败返回 None。"""
    try:
        route = resolve_s1_ai_profile_snapshot('description_backfill')
    except AIProfileAssignmentError as exc:
        logger.warning(
            '描述补全 AI 用途路由不可用 (log=%s): %s',
            expedition_log_id,
            exc,
        )
        return None
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout = int(current_app.config.get('ADVENTURER_S1_DESC_BACKFILL_AI_TIMEOUT_SECONDS', 600) or 600)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    usage = None
    usage_deferred = False
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None
    allowed_keys = {
        (str(item.get('type')), int(item.get('id')))
        for item in items
        if item.get('type') in ('trait', 'skill') and item.get('id') is not None
    }

    def validate_desc_backfill_response(
        decoded,
    ) -> S1AiResponseContractResult:
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json',
                failure_stage='response_parse',
            )
        _, contract_valid = _parse_desc_backfill_results_with_status(
            decoded.json_result.data,
            allowed_keys=allowed_keys,
        )
        if contract_valid:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(
            'description_backfill_contract_invalid',
            failure_stage='contract_validation',
        )

    try:
        # 长 HTTP 调用前释放 DB 连接，避免 AI 等待期间占用连接池（对齐 expedition_runner）
        db_manager.remove()
        resp = post_s1_ai_request(
            f"{api_base}/chat/completions",
            feature='description_backfill',
            resolved_profile=route,
            request_metadata_out=request_metadata,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _DESC_BACKFILL_SYSTEM_PROMPT},
                    {"role": "user", "content": _build_desc_backfill_user_prompt(items)},
                ],
                "temperature": 0.3,
            },
            timeout=timeout,
            response_validator=validate_desc_backfill_response,
        )
        decoded = parse_s1_ai_http_json_response(resp)
        usage = decoded.http_result.usage
        if not decoded.json_result.ok:
            raise ValueError(
                f'描述补全响应不可用: {decoded.json_result.error_code}'
            )

        result, contract_valid = _parse_desc_backfill_results_with_status(
            decoded.json_result.data,
            allowed_keys=allowed_keys,
        )
        if result is not None:
            deferred_token_usage = (
                _DeferredDescBackfillTokenUsage(
                    usage=usage,
                    model=request_metadata.get('model') or model,
                    expedition_log_id=expedition_log_id,
                )
                if isinstance(usage, dict) and usage
                else None
            )
            usage_deferred = deferred_token_usage is not None
            return _DescBackfillAIResult(
                result,
                deferred_token_usage=deferred_token_usage,
                contract_valid=contract_valid,
            )
        return None

    except Exception as e:
        request_exception = e
        logger.warning('描述补全 AI 调用失败 (log=%s): %s', expedition_log_id, e)
        return None
    finally:
        if (usage or request_exception is not None) and not usage_deferred:
            record_token_usage(
                call_type='desc_backfill_ai',
                usage=usage,
                model=request_metadata.get('model') or model,
                expedition_log_id=expedition_log_id,
                attempt_number=1,
                is_success=False,
                failure_stage='http_or_response',
                validation_error_code=(
                    f'http_or_response.{type(request_exception).__name__}'
                    if request_exception is not None
                    else 'description_backfill_invalid'
                ),
                record_without_usage=request_exception is not None,
                request_metadata=request_metadata,
                request_exception=request_exception,
            )


def _collect_empty_desc_items(log: AdventurerS1ExpeditionLog) -> list[dict]:
    """从探险日志的 member_updates 取出缺失描述的特质和技能。

    返回 [{type, id, name, adventurer_name, dungeon_theme, evidence}]。
    """
    member_updates = (log.full_log or {}).get('member_updates', []) or []

    # 收集本次涉及的冒险者 id 和 new_traits / new_skills 名
    adv_ids: set[int] = set()
    trait_names_by_adv: dict[int, set[str]] = {}
    skill_names_by_adv: dict[int, set[str]] = {}
    member_update_by_adv: dict[int, dict] = {}

    for u in member_updates:
        if not isinstance(u, dict):
            continue
        try:
            adv_id = int(u.get('id'))
        except (TypeError, ValueError):
            continue
        if not adv_id:
            continue
        member_update_by_adv[adv_id] = u

        raw_traits = u.get('new_traits')
        if raw_traits:
            if isinstance(raw_traits, dict):
                names = {str(n).strip() for n in raw_traits.keys() if str(n).strip()}
            elif isinstance(raw_traits, list):
                names = set()
                for t in raw_traits:
                    if isinstance(t, dict):
                        n = str(t.get('name') or '').strip()
                        if n:
                            names.add(n)
                    elif isinstance(t, str) and t.strip():
                        names.add(t.strip())
            else:
                names = set()
            if names:
                adv_ids.add(adv_id)
                trait_names_by_adv.setdefault(adv_id, set()).update(names)

        raw_skills = u.get('new_skills')
        if raw_skills and isinstance(raw_skills, list):
            names = set()
            for s in raw_skills:
                if isinstance(s, dict):
                    n = str(s.get('name') or '').strip()
                    if n:
                        names.add(n)
            if names:
                adv_ids.add(adv_id)
                skill_names_by_adv.setdefault(adv_id, set()).update(names)

    if not adv_ids:
        return []

    # 批量查库，找 description 为空的
    from plugins.adventurer_s1.models import AdventurerS1

    adv_name_map = {
        a.id: a.name
        for a in AdventurerS1.query.filter(AdventurerS1.id.in_(list(adv_ids))).all()
    }

    items: list[dict] = []
    dungeon_theme = log.target_dungeon or ''
    full_log = log.full_log or {}

    def evidence_for(adv_id: int) -> dict:
        update = member_update_by_adv.get(adv_id) or {}
        evidence = {
            'all_events': full_log.get('events') or [],
            'expedition_summary': full_log.get('summary') or log.result_summary or '',
            'member_history_story_append': update.get('history_story_append') or '',
            'member_new_traits': update.get('new_traits') or {},
            'member_new_skills': update.get('new_skills') or [],
            'member_new_equipments': update.get('new_equipments') or {},
            'member_removed_skills': update.get('removed_skills') or [],
        }
        fate_context = full_log.get('fate_journey_context')
        fate_progress = full_log.get('fate_progress')
        try:
            fate_adventurer_id = int((fate_context or {}).get('adventurer_id') or 0)
        except (AttributeError, TypeError, ValueError):
            fate_adventurer_id = 0
        if isinstance(fate_context, dict) and fate_adventurer_id == adv_id:
            evidence['fate_journey_context'] = fate_context
            evidence['fate_progress'] = fate_progress if isinstance(fate_progress, dict) else {}
        return evidence

    # 与结算写入器采用同一名称规范化，避免带括注的旧名称漏选。
    from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
        normalized_member_new_trait_names,
    )
    trait_names_by_adv = {
        adv_id: normalized_member_new_trait_names(
            member_update_by_adv[adv_id].get('new_traits'),
            member_name=adv_name_map.get(adv_id),
        )
        for adv_id in trait_names_by_adv
    }

    # 特质
    all_trait_names: set[str] = set()
    for names in trait_names_by_adv.values():
        all_trait_names |= names
    if all_trait_names:
        traits = (
            AdventurerS1Trait.query
            .filter(
                AdventurerS1Trait.adventurer_id.in_(list(trait_names_by_adv.keys())),
                AdventurerS1Trait.name.in_(list(all_trait_names)),
                AdventurerS1Trait.is_archived == False,  # noqa: E712
            )
            .all()
        )
        for t in traits:
            if t.name in trait_names_by_adv.get(t.adventurer_id, set()):
                desc = (t.description or '').strip()
                if is_missing_asset_description('trait', desc):
                    items.append({
                        'type': 'trait',
                        'id': t.id,
                        'name': t.name,
                        'adventurer_id': t.adventurer_id,
                        'source_description': t.description,
                        'adventurer_name': adv_name_map.get(t.adventurer_id, ''),
                        'dungeon_theme': dungeon_theme,
                        'evidence': evidence_for(int(t.adventurer_id)),
                    })

    # 技能
    all_skill_names: set[str] = set()
    for names in skill_names_by_adv.values():
        all_skill_names |= names
    if all_skill_names:
        skills = (
            AdventurerS1Skill.query
            .filter(
                AdventurerS1Skill.adventurer_id.in_(list(skill_names_by_adv.keys())),
                AdventurerS1Skill.name.in_(list(all_skill_names)),
            )
            .all()
        )
        for s in skills:
            if s.name in skill_names_by_adv.get(s.adventurer_id, set()):
                desc = (s.description or '').strip()
                if is_missing_asset_description('skill', desc):
                    items.append({
                        'type': 'skill',
                        'id': s.id,
                        'name': s.name,
                        'adventurer_id': s.adventurer_id,
                        'source_description': s.description,
                        'adventurer_name': adv_name_map.get(s.adventurer_id, ''),
                        'dungeon_theme': dungeon_theme,
                        'evidence': evidence_for(int(s.adventurer_id)),
                    })

    return items


def run_s1_desc_backfill_ai(expedition_log_id: int) -> None:
    """AsyncTaskService worker 入口：为缺失描述的特质/技能补全描述。

    流程：只读取目标条目 → 调补全 AI（不持有事务）→ 新事务按 id 重新加载写回。
    AI 失败时不动条目（保留现有描述），任务结束。
    """
    log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, expedition_log_id)
    if not log:
        logger.info('描述补全 AI 跳过：探险日志不存在 log=%s', expedition_log_id)
        return

    items = _collect_empty_desc_items(log)
    if not items:
        logger.info('描述补全 AI 跳过：本次无缺失描述 log=%s', expedition_log_id)
        return

    db_manager.remove()
    mapping = _call_desc_backfill_ai(items, expedition_log_id=expedition_log_id)
    if not mapping:
        logger.warning(
            '描述补全 AI 无有效返回，保留现有描述 log=%s items=%d',
            expedition_log_id, len(items),
        )
        return

    # 按类型分组写回
    trait_ids = [item_id for (t, item_id) in mapping if t == 'trait']
    skill_ids = [item_id for (t, item_id) in mapping if t == 'skill']

    deferred_token_usage = getattr(mapping, 'deferred_token_usage', None)
    contract_valid = getattr(mapping, 'contract_valid', True) is True
    expected_keys = {
        (item['type'], item['id'])
        for item in items
        if isinstance(item, dict)
        and item.get('type') in ('trait', 'skill')
        and type(item.get('id')) is int
        and item['id'] > 0
    }
    applied_keys: set[tuple[str, int]] = set()
    consumer_succeeded = False
    frozen_items = {(item['type'], item['id']): item for item in items}

    def can_write(kind, row, desc):
        frozen = frozen_items.get((kind, row.id))
        return bool(
            frozen
            and row.adventurer_id == frozen['adventurer_id']
            and row.name == frozen['name']
            and row.description == frozen['source_description']
            and not getattr(row, 'is_archived', False)
            and is_missing_asset_description(kind, row.description)
            and isinstance(desc, str)
            and not is_missing_asset_description(kind, desc)
        )

    try:
        with db_manager.transaction():
            if trait_ids:
                traits = (
                    AdventurerS1Trait.query.filter(AdventurerS1Trait.id.in_(trait_ids))
                    .order_by(AdventurerS1Trait.id).populate_existing().with_for_update().all()
                )
                for t in traits:
                    desc = mapping.get(('trait', t.id))
                    if can_write('trait', t, desc):
                        from plugins.adventurer_s1.services.s1_trait_description_ai_service import (
                            sync_trait_description_fields,
                        )

                        sync_trait_description_fields(t, desc)
                        applied_keys.add(('trait', t.id))

            if skill_ids:
                skills = (
                    AdventurerS1Skill.query.filter(AdventurerS1Skill.id.in_(skill_ids))
                    .order_by(AdventurerS1Skill.id).populate_existing().with_for_update().all()
                )
                for s in skills:
                    desc = mapping.get(('skill', s.id))
                    if can_write('skill', s, desc):
                        s.description = desc
                        applied_keys.add(('skill', s.id))
        consumer_succeeded = (
            contract_valid
            and bool(expected_keys)
            and set(mapping) == expected_keys
            and applied_keys == expected_keys
        )
    finally:
        if deferred_token_usage is not None:
            try:
                deferred_token_usage.finalize(is_success=consumer_succeeded)
            except Exception:
                logger.warning(
                    '描述补全 AI token 落账失败 (log=%s success=%s)',
                    expedition_log_id,
                    consumer_succeeded,
                    exc_info=True,
                )

    logger.info(
        '描述补全 AI 完成 log=%s applied=%d/%d',
        expedition_log_id, len(applied_keys), len(items),
    )
