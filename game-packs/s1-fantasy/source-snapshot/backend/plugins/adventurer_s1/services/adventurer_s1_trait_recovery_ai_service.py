"""深渊酒馆 S1 负面特质恢复 AI 服务。

探险主 AI 不再产出恢复字段（policy/stage/required/recovered_desc）；探险结算后，
本服务由 AsyncTaskService worker 调用一个轻量恢复 AI，为本次新增/修改的特质回填
结构化恢复剧本，之后由 adventurer_s1_trait_recovery_service 纯后端逻辑结算自然恢复。

设计要点：
- 输入输出精简：只喂特质的 id / name / description，省 token。
- 自然恢复只移除「纯负面」特质；好坏参半或痊愈后应留正面沉淀的特质强制 policy=none，
  避免后端整条移除时连正面部分一起删掉。
- 失败兜底：AI 无有效返回时特质保持 policy=none（不自然恢复），可被 stuck 补投重试。
"""
import json
import logging

import requests

from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    AIProfileAssignmentError,
    post_s1_ai_request,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
)
from flask import current_app

from db_repository import db_manager
from plugins.adventurer.services.adventurer_log_service import _normalize_member_new_traits_payload
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1Trait,
)
from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
    NATURAL_RECOVERY_POLICIES,
    TRACE_RECOVERY_HOURS_MAX,
    TRACE_RECOVERY_HOURS_MIN,
    apply_recovery_metadata_to_trait,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage

logger = logging.getLogger(__name__)


class _DeferredTraitRecoveryTokenUsage:
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
            call_type='trait_recovery_ai',
            usage=self._usage,
            model=self._model,
            expedition_log_id=self._expedition_log_id,
            attempt_number=1,
            is_success=is_success,
        )


class _TraitRecoveryAIResult(dict):
    """Keep the public id mapping while carrying deferred usage state."""

    def __init__(
        self,
        value: dict[int, dict],
        *,
        deferred_token_usage: _DeferredTraitRecoveryTokenUsage | None,
        contract_valid: bool,
    ) -> None:
        super().__init__(value)
        self.deferred_token_usage = deferred_token_usage
        self.contract_valid = contract_valid is True


_TRAIT_RECOVERY_SYSTEM_PROMPT = """你是深渊酒馆的"特质恢复评估员"。给你一批冒险者刚在探险中新获得或被改写的特质，你要逐个判断：它能否自然消退，以及恢复的节奏。后端只按你输出的结构化标签结算，绝不会读描述文字去猜。

【只有这两类算"可自然恢复"】
- policy=rest（静养型：轻微外伤、淤青等）
- policy=sleep（睡眠型：疲劳、精神透支、轻度惊吓等）
其余情况一律 policy=none。

【恢复阶段（按轻重选用，不要一律标 light）】
- stage=trace，recovery_hours=1~4：极轻痕迹（磕碰、浅淤青、短暂惊吓、活动一下就好）。墙钟 1~4 小时后自动消退，不占用宿舍床位，不阻止探险。若描述里已有恶化、扩散、需固定/换药/医疗处置，禁止用 trace。
- stage=light，required=1：比 trace 重、但仍可在宿舍睡满 1 个周期（6 小时）后消失。
- stage=medium，required=2（偏重可给 3）：需持续静养换药，占宿舍床位；第 1 周期后描述更新为 recovered_desc，最后 1 周期后消失。
- recovered_desc：仅 stage=medium 时填写；trace/light/none 不要填。

【必须 policy=none 的情况（重点）】
1. 好坏参半：有正面沉淀的特质禁止自然恢复（整条移除会删掉正面部分）。
2. 重伤/恶化/顽疾：贯穿伤、感染扩散、需正规医疗、诅咒污染等。
3. 进本后若同一条伤在叙事里明显恶化，只有仍可通过持续静养恢复时才标 medium；需要正规医疗或不属于纯负面短期状态时标 none。
4. 正面/中性/长期特质，或拿不准是否纯负面时，一律 policy=none。

【输出格式】
只返回合法 JSON。对每一个 id 都要给一条结果：
{
  "results": [
    {"id": 11, "policy": "rest", "stage": "trace", "recovery_hours": 2},
    {"id": 12, "policy": "rest", "stage": "light", "required": 1, "recovered_desc": null},
    {"id": 13, "policy": "sleep", "stage": "medium", "required": 2, "recovered_desc": "..."},
    {"id": 14, "policy": "none"}
  ]
}
"""


def _build_recovery_user_prompt(items: list[dict]) -> str:
    payload = [
        {'id': it['id'], 'name': it['name'], 'description': it['description']}
        for it in items
    ]
    return (
        "请为以下每个负面特质判断恢复方案，按 id 一一对应输出 results。\n"
        "特质列表：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _safe_recovery_metadata() -> dict:
    """Return the fail-closed metadata used for one identifiable invalid row."""
    return {
        'policy': 'none',
        'stage': None,
        'required': None,
        'recovered_desc': None,
        'recovery_hours': None,
        'hours': None,
    }


def _parse_recovery_results_with_status(
    parsed,
    *,
    allowed_ids: set[int] | None = None,
) -> tuple[dict[int, dict] | None, bool]:
    """解析恢复元数据，并独立返回整批输出契约是否完整有效。"""
    if not isinstance(parsed, dict):
        return None, False
    exact_allowed_ids = (
        {
            trait_id for trait_id in allowed_ids
            if type(trait_id) is int and trait_id > 0
        }
        if allowed_ids is not None
        else None
    )
    results = parsed.get('results')
    if isinstance(results, dict):
        singleton_id, _ = normalize_ascii_integer(results.get('id'), minimum=1)
        if (
            exact_allowed_ids is None
            or len(exact_allowed_ids) != 1
            or singleton_id not in exact_allowed_ids
        ):
            return None, False
        results = [results]
    elif not isinstance(results, list):
        return None, False

    by_id: dict[int, dict] = {}
    seen_ids: set[int] = set()
    contract_valid = True
    for entry in results:
        if not isinstance(entry, dict):
            contract_valid = False
            continue
        tid, _ = normalize_ascii_integer(entry.get('id'), minimum=1)
        if type(tid) is not int or tid <= 0:
            contract_valid = False
            continue
        if exact_allowed_ids is not None and tid not in exact_allowed_ids:
            contract_valid = False
            continue
        if tid in seen_ids:
            contract_valid = False
            by_id.pop(tid, None)
            continue
        seen_ids.add(tid)

        numeric_fields: dict[str, int | None] = {}
        invalid_integer = False
        for field in ('required', 'recovery_hours', 'hours'):
            raw_value = entry.get(field)
            normalized_value, _ = normalize_ascii_integer(raw_value)
            if raw_value is not None and type(normalized_value) is not int:
                invalid_integer = True
                break
            numeric_fields[field] = normalized_value
        if invalid_integer:
            contract_valid = False
            by_id[tid] = _safe_recovery_metadata()
            continue

        raw_policy = entry.get('policy')
        raw_stage = entry.get('stage')
        raw_recovered_desc = entry.get('recovered_desc')
        if not isinstance(raw_policy, str):
            contract_valid = False
            by_id[tid] = _safe_recovery_metadata()
            continue
        policy = raw_policy.strip().lower()
        stage = None
        if raw_stage is not None:
            if not isinstance(raw_stage, str):
                contract_valid = False
                by_id[tid] = _safe_recovery_metadata()
                continue
            stage = raw_stage.strip().lower() or None
        if raw_recovered_desc is not None and not isinstance(raw_recovered_desc, str):
            contract_valid = False
            by_id[tid] = _safe_recovery_metadata()
            continue
        recovered_desc = (
            raw_recovered_desc.strip()
            if isinstance(raw_recovered_desc, str) and raw_recovered_desc.strip()
            else None
        )

        required = numeric_fields['required']
        recovery_hours = numeric_fields['recovery_hours']
        hours_alias = numeric_fields['hours']
        if (
            recovery_hours is not None
            and hours_alias is not None
            and recovery_hours != hours_alias
        ):
            contract_valid = False
            by_id[tid] = _safe_recovery_metadata()
            continue
        effective_hours = recovery_hours if recovery_hours is not None else hours_alias

        row_valid = False
        if policy == 'none':
            row_valid = (
                stage is None
                and required is None
                and effective_hours is None
                and recovered_desc is None
            )
        elif policy in NATURAL_RECOVERY_POLICIES and stage == 'trace':
            row_valid = (
                required is None
                and type(effective_hours) is int
                and TRACE_RECOVERY_HOURS_MIN
                <= effective_hours
                <= TRACE_RECOVERY_HOURS_MAX
                and recovered_desc is None
            )
        elif policy in NATURAL_RECOVERY_POLICIES and stage == 'light':
            row_valid = (
                required == 1
                and effective_hours is None
                and recovered_desc is None
            )
        elif policy in NATURAL_RECOVERY_POLICIES and stage == 'medium':
            row_valid = (
                required in {2, 3}
                and effective_hours is None
                and recovered_desc is not None
            )

        if not row_valid:
            contract_valid = False
            by_id[tid] = _safe_recovery_metadata()
            continue
        by_id[tid] = {
            'policy': policy,
            'stage': stage,
            'required': required,
            'recovered_desc': recovered_desc,
            'recovery_hours': effective_hours,
            'hours': None,
        }

    if exact_allowed_ids is not None and set(by_id) != exact_allowed_ids:
        contract_valid = False
    return by_id or None, bool(by_id) and contract_valid


def _parse_recovery_results(
    parsed,
    *,
    allowed_ids: set[int] | None = None,
) -> dict[int, dict] | None:
    """兼容旧调用：只返回可安全消费的逐条恢复元数据。"""
    result, _contract_valid = _parse_recovery_results_with_status(
        parsed,
        allowed_ids=allowed_ids,
    )
    return result


def _call_trait_recovery_ai(items: list[dict], expedition_log_id: int | None = None) -> dict[int, dict] | None:
    """调用恢复 AI，返回 {trait_id: 恢复元数据}；失败返回 None。"""
    try:
        route = resolve_s1_ai_profile_snapshot('trait_recovery')
    except AIProfileAssignmentError as exc:
        logger.warning(
            '恢复 AI 用途路由不可用 (log=%s): %s',
            expedition_log_id,
            exc,
        )
        return None
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout = int(current_app.config.get('ADVENTURER_S1_TRAIT_RECOVERY_AI_TIMEOUT_SECONDS', 600) or 600)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    usage = None
    usage_deferred = False
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None
    allowed_ids = {
        item['id']
        for item in items
        if isinstance(item, dict)
        and type(item.get('id')) is int
        and item['id'] > 0
    }
    if not allowed_ids:
        logger.warning('恢复 AI 跳过：本批没有合法特质 ID (log=%s)', expedition_log_id)
        return None

    def validate_trait_recovery_response(
        decoded,
    ) -> S1AiResponseContractResult:
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json',
                failure_stage='response_parse',
            )
        _, contract_valid = _parse_recovery_results_with_status(
            decoded.json_result.data,
            allowed_ids=allowed_ids,
        )
        if contract_valid:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(
            'trait_recovery_contract_invalid',
            failure_stage='contract_validation',
        )

    try:
        # 长 HTTP 调用前释放 DB 连接，避免 AI 等待期间占用连接池（对齐 expedition_runner）
        db_manager.remove()
        resp = post_s1_ai_request(
            f"{api_base}/chat/completions",
            feature='trait_recovery',
            resolved_profile=route,
            request_metadata_out=request_metadata,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _TRAIT_RECOVERY_SYSTEM_PROMPT},
                    {"role": "user", "content": _build_recovery_user_prompt(items)},
                ],
                "temperature": 0.2,
            },
            timeout=timeout,
            response_validator=validate_trait_recovery_response,
        )
        decoded = parse_s1_ai_http_json_response(resp)
        usage = decoded.http_result.usage
        if not decoded.json_result.ok:
            raise ValueError(
                f'恢复 AI 响应不可用: {decoded.json_result.error_code}'
            )

        result, contract_valid = _parse_recovery_results_with_status(
            decoded.json_result.data,
            allowed_ids=allowed_ids,
        )
        if result is not None:
            deferred_token_usage = (
                _DeferredTraitRecoveryTokenUsage(
                    usage=usage,
                    model=request_metadata.get('model') or model,
                    expedition_log_id=expedition_log_id,
                )
                if isinstance(usage, dict) and usage
                else None
            )
            usage_deferred = deferred_token_usage is not None
            return _TraitRecoveryAIResult(
                result,
                deferred_token_usage=deferred_token_usage,
                contract_valid=contract_valid,
            )
        return None

    except Exception as e:
        request_exception = e
        logger.warning('恢复 AI 调用失败 (log=%s): %s', expedition_log_id, e)
        return None
    finally:
        if (usage or request_exception is not None) and not usage_deferred:
            record_token_usage(
                call_type='trait_recovery_ai',
                usage=usage,
                model=request_metadata.get('model') or model,
                expedition_log_id=expedition_log_id,
                attempt_number=1,
                is_success=False,
                failure_stage='http_or_response',
                validation_error_code=(
                    f'http_or_response.{type(request_exception).__name__}'
                    if request_exception is not None
                    else 'trait_recovery_invalid'
                ),
                record_without_usage=request_exception is not None,
                request_metadata=request_metadata,
                request_exception=request_exception,
            )


def _collect_target_traits(log: AdventurerS1ExpeditionLog) -> list[AdventurerS1Trait]:
    """从探险日志的 member_updates 取出本次新增/修改的未归档特质。

    用与探险结算同一套 _normalize_member_new_traits_payload 规范化名字，保证与入库
    特质名一致；批量 IN 查询避免 N+1。
    """
    member_updates = (log.full_log or {}).get('member_updates', []) or []

    raw_by_adv: dict[int, list] = {}
    for u in member_updates:
        if not isinstance(u, dict):
            continue
        adv_id, _ = normalize_ascii_integer(u.get('id'), minimum=1)
        if type(adv_id) is not int or adv_id <= 0:
            continue
        raw_new = u.get('new_traits')
        if not raw_new:
            continue
        raw_by_adv.setdefault(adv_id, []).append(raw_new)

    if not raw_by_adv:
        return []

    adv_name_map = {
        a.id: a.name
        for a in AdventurerS1.query.filter(AdventurerS1.id.in_(list(raw_by_adv.keys()))).all()
    }

    names_by_adv: dict[int, set[str]] = {}
    for adv_id, raw_list in raw_by_adv.items():
        for raw_new in raw_list:
            norm = _normalize_member_new_traits_payload(
                raw_new, {}, member_name=adv_name_map.get(adv_id),
            )
            if norm:
                names_by_adv.setdefault(adv_id, set()).update(norm.keys())

    if not names_by_adv:
        return []

    all_names: set[str] = set()
    for names in names_by_adv.values():
        all_names |= names

    traits = (
        AdventurerS1Trait.query
        .filter(
            AdventurerS1Trait.adventurer_id.in_(list(names_by_adv.keys())),
            AdventurerS1Trait.name.in_(list(all_names)),
            AdventurerS1Trait.is_archived == False,  # noqa: E712
        )
        .all()
    )
    return [t for t in traits if t.name in names_by_adv.get(t.adventurer_id, set())]


def run_s1_trait_recovery_ai(expedition_log_id: int, party_id: int | None = None) -> None:
    """AsyncTaskService worker 入口：为一次探险新增/修改的特质回填恢复剧本。

    流程：只读取出目标特质 → 调恢复 AI（不持有事务）→ 新事务按 id 重新加载写回。
    AI 失败时不动特质（保持 policy=none），任务结束，可被 stuck 补投重试。
    """
    log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, expedition_log_id)
    if not log:
        logger.info('恢复 AI 跳过：探险日志不存在 log=%s', expedition_log_id)
        return

    target_traits = _collect_target_traits(log)
    if not target_traits:
        logger.info('恢复 AI 跳过：本次无新增/修改特质 log=%s', expedition_log_id)
        return

    items = [
        {'id': t.id, 'name': t.name, 'description': t.description or ''}
        for t in target_traits
    ]
    target_ids = [t.id for t in target_traits]

    db_manager.remove()
    metadata_by_id = _call_trait_recovery_ai(items, expedition_log_id=expedition_log_id)
    if not metadata_by_id:
        logger.warning(
            '恢复 AI 无有效返回，特质保持 policy=none log=%s traits=%s',
            expedition_log_id, target_ids,
        )
        return

    deferred_token_usage = getattr(metadata_by_id, 'deferred_token_usage', None)
    contract_valid = getattr(metadata_by_id, 'contract_valid', True) is True
    expected_ids = set(target_ids)
    applied_ids: set[int] = set()
    consumer_succeeded = False
    try:
        with db_manager.transaction():
            rows = (
                AdventurerS1Trait.query
                .filter(AdventurerS1Trait.id.in_(target_ids))
                .all()
            )
            member_by_id = {
                int(row.adventurer_id): row.adventurer
                for row in rows
                if getattr(row, 'adventurer_id', None) is not None and row.adventurer is not None
            }
            for t in rows:
                if t.is_archived:
                    continue
                meta = metadata_by_id.get(t.id)
                if meta is None:
                    continue
                apply_recovery_metadata_to_trait(
                    t,
                    meta,
                    member=member_by_id.get(int(t.adventurer_id)),
                )
                applied_ids.add(t.id)
        consumer_succeeded = (
            contract_valid
            and bool(expected_ids)
            and set(metadata_by_id) == expected_ids
            and applied_ids == expected_ids
        )
    finally:
        if deferred_token_usage is not None:
            try:
                deferred_token_usage.finalize(is_success=consumer_succeeded)
            except Exception:
                logger.warning(
                    '恢复 AI token 落账失败 (log=%s success=%s)',
                    expedition_log_id,
                    consumer_succeeded,
                    exc_info=True,
                )

    logger.info(
        '恢复 AI 回填完成 log=%s applied=%s/%s',
        expedition_log_id, len(applied_ids), len(target_ids),
    )
