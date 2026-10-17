"""S1 AI JSON 响应的统一提取、语法修复与结构校验工具。

本模块只处理供应商响应形状和 JSON 结构，不承担任何剧情、审核、结算或授权语义。
业务服务必须在 ``data`` 解析成功后继续执行自己的精确契约校验。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal

import json_repair
from flask import current_app, has_app_context
from pydantic import BaseModel, ValidationError
from utils.ai_field_names import normalize_ai_field_names
from plugins.adventurer_s1.services.s1_ai_failure_evidence import (
    S1_AI_FAILURE_EVIDENCE_KEY,
    S1AiFailureEvidence,
    get_s1_ai_failure_evidence,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer


S1AiJsonRepairMode = Literal['none', 'syntax_only', 'lossless_syntax_only']
S1AiHttpRecoveryMode = Literal[
    'strict_envelope',
    'repaired_envelope',
    'direct_body_candidate',
    'failed',
]
S1_AI_REASONING_FIRST_SENTENCE_LIMIT = 500
S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY = (
    '_s1_ai_physical_attempt_receipts'
)
S1_AI_ROUTE_METADATA_KEY = '_s1_ai_route_metadata'
S1_AI_ROUTE_METADATA_FIELDS = (
    'profile_id',
    'primary_profile_id',
    'backup_profile_id',
    'backup_profile_resolution',
    'provider',
    'api_key_slot',
    'api_endpoint_slot',
    'endpoint_host',
    'transport_attempt',
    'failover_reason',
    'failover_scope',
    'failover_attempt_count',
    'request_timeout_mode',
    'request_inactivity_timeout_seconds',
    'request_deadline_mode',
    'request_deadline_seconds',
    'request_primary_window_seconds',
    'request_failover_window_activated',
    'request_failover_attempt_started',
    'request_deadline_extension_count',
    'request_deadline_failover_window_seconds',
    'request_deadline_exhausted',
    'request_elapsed_ms',
    'stream_deadline_mode',
    'stream_idle_timeout_seconds',
    'stream_transport_activity_count',
    'stream_progress_event_count',
    'stream_idle_deadline_exhausted',
    'response_body_inactivity_timeout_seconds',
    'response_body_transport_activity_count',
    'response_body_inactivity_timeout_exhausted',
    'response_body_read_mode',
)
S1_AI_VISIBLE_REASONING_CHARS_KEY = '_s1_ai_visible_reasoning_chars'
_S1_AI_SENTENCE_ENDINGS = frozenset('.!?。！？')


class _DuplicateJsonKeyError(ValueError):
    pass


def build_s1_ai_raw_excerpt(text: str, limit: int = 2000) -> str:
    """保留响应首尾，便于诊断截断输出且避免日志无限膨胀。"""
    value = str(text or '')
    if len(value) <= limit:
        return value
    marker = '\n...（中间内容已省略）...\n'
    remaining = max(0, limit - len(marker))
    head_length = remaining // 2
    return f'{value[:head_length]}{marker}{value[-(remaining - head_length):]}'


def extract_s1_ai_reasoning_content(response_json: object) -> str | None:
    """安全提取 provider reasoning_content，仅供内部诊断。"""
    if not isinstance(response_json, dict):
        return None
    choices = response_json.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get('message')
    if not isinstance(message, dict):
        return None
    raw_reasoning = message.get('reasoning_content')
    if not isinstance(raw_reasoning, str):
        return None
    reasoning = raw_reasoning.strip()
    return reasoning or None


def extract_s1_ai_structured_refusal(response_json: object) -> str | None:
    """提取 provider 的结构化 refusal 字段，不分析正文语义。"""
    if not isinstance(response_json, dict):
        return None
    choices = response_json.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get('message')
    if not isinstance(message, dict):
        return None
    raw_refusal = message.get('refusal')
    if not isinstance(raw_refusal, str):
        return None
    refusal = raw_refusal.strip()
    return refusal or None


def extract_s1_ai_visible_reasoning_chars(response_json: object) -> int | None:
    """统计 provider 实际返回的可见 reasoning_content 字符数。

    ``None`` 表示响应没有可统计的字符串字段；``0`` 表示字段存在但内容为空。
    这是响应字符数，不等同于 provider 的计费或推理 token。
    """
    if not isinstance(response_json, dict):
        return None
    choices = response_json.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get('message')
    if not isinstance(message, dict) or 'reasoning_content' not in message:
        return None
    raw_reasoning = message.get('reasoning_content')
    if not isinstance(raw_reasoning, str):
        return None
    return len(raw_reasoning.strip())


def extract_s1_ai_reasoning_first_sentence(
    response_json: object,
    *,
    limit: int = S1_AI_REASONING_FIRST_SENTENCE_LIMIT,
) -> str | None:
    """提取 provider reasoning_content 的有界首句，仅供管理员诊断。"""
    raw_reasoning = extract_s1_ai_reasoning_content(response_json)
    if raw_reasoning is None:
        return None

    reasoning = ' '.join(raw_reasoning.split())
    if not reasoning:
        return None

    bounded_limit = max(1, int(limit))
    sentence_end = len(reasoning)
    for index, character in enumerate(reasoning):
        if character in _S1_AI_SENTENCE_ENDINGS:
            sentence_end = index + 1
            break
    return reasoning[:min(sentence_end, bounded_limit)]


@dataclass(frozen=True)
class S1AiJsonResult:
    data: dict | None = None
    content: str = ''
    error_code: str | None = None
    error_detail: str | None = None
    raw_excerpt: str = ''
    finish_reason: str | None = None
    repair_applied: bool = False
    validation_errors: list[dict] = field(default_factory=list)
    field_name_diagnostics: dict = field(default_factory=dict)
    key_integrity_verified: bool = False
    evidence: S1AiFailureEvidence | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error_code is None


def apply_s1_ai_field_schema(
    result: S1AiJsonResult, *, field_schema: dict, contract: str,
) -> S1AiJsonResult:
    """Normalize raw keys before the owner's projection; never validate business facts."""
    if not result.ok:
        return result
    compatible = normalize_ai_field_names(result.data, field_schema)
    if not compatible.repairs and not compatible.issues:
        return result
    if compatible.repairs and not result.key_integrity_verified:
        # The legacy permissive syntax repairer can discard duplicate keys or
        # fabricate values. Its output alone cannot authorize a spelling repair.
        compatible.issues.append({
            'path': '$', 'reason': 'source_key_integrity_unverified',
            'rule': 'lossless_json_source_required',
        })
        compatible.data = None
    diagnostics = {
        'contract': contract,
        'repairs': compatible.repairs,
        'issues': compatible.issues,
    }
    if result.evidence is not None:
        result.evidence.record_field_name_compatibility(diagnostics)
    return replace(
        result, data=compatible.data,
        error_code='field_name_compatibility_failed' if compatible.issues else None,
        error_detail=compatible.issues[0]['reason'] if compatible.issues else None,
        field_name_diagnostics=diagnostics,
    )


@dataclass(frozen=True)
class S1AiHttpResponseResult:
    """HTTP 成功响应的信封解码结果；正文仍只是待业务校验候选。"""

    response_json: dict | None
    content_result: S1AiJsonResult
    usage: dict | None = None
    recovery_mode: S1AiHttpRecoveryMode = 'failed'
    raw_excerpt: str = ''
    runtime_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def content(self) -> str:
        return self.content_result.content

    @property
    def error_code(self) -> str | None:
        return self.content_result.error_code

    @property
    def finish_reason(self) -> str | None:
        return self.content_result.finish_reason

    @property
    def repair_applied(self) -> bool:
        return self.content_result.repair_applied

    @property
    def has_content_candidate(self) -> bool:
        """只表示存在正文候选，不代表调用或业务消费成功。"""
        return bool(self.content) and self.error_code is None


@dataclass(frozen=True)
class S1AiHttpJsonResponseResult:
    """统一 HTTP 解码结果及其 JSON/schema 解析结果。"""

    http_result: S1AiHttpResponseResult
    json_result: S1AiJsonResult


@dataclass(frozen=True)
class S1AiResponseContractResult:
    """一次纯解析/校验消费的结果，供角色验收和真实断流候选保留判定。

    ``accepted`` 只表示当前响应已经通过该 AI 角色的无副作用交付合同。
    已定义逐条独立消费的批任务可交付经严格校验保留的完整子项；批次缺项仍由
    owner 记录并重试，不改变其它角色的整体闭合要求。
    合法的业务拒绝、审核不通过或空操作也必须返回 ``accepted=True``；网络、
    信封、JSON、schema 或角色 validator 失败返回 ``accepted=False``。
    输出校验失败本身不触发备用路由；原 owner 沿已有预算处理，业务拒绝不重审求通过。
    数据库写回、任务 claim 与其它有副作用消费不得放进本结果的生产回调中。
    """

    accepted: bool
    value: Any = None
    error_code: str | None = None
    failure_stage: str = 'contract_validation'
    diagnostics: dict[str, Any] | None = None

    @classmethod
    def accept(cls, value: Any = None) -> 'S1AiResponseContractResult':
        return cls(accepted=True, value=value)

    @classmethod
    def reject(
        cls,
        error_code: str,
        *,
        failure_stage: str = 'contract_validation',
        diagnostics: dict[str, Any] | None = None,
    ) -> 'S1AiResponseContractResult':
        normalized_error_code = str(error_code or '').strip()
        if not normalized_error_code:
            raise ValueError('拒绝 AI 响应时必须提供 error_code')
        normalized_failure_stage = str(failure_stage or '').strip()
        if not normalized_failure_stage:
            raise ValueError('拒绝 AI 响应时必须提供 failure_stage')
        return cls(
            accepted=False,
            error_code=normalized_error_code,
            failure_stage=normalized_failure_stage,
            diagnostics=diagnostics,
        )


def _error_result(
    error_code: str,
    *,
    content: str = '',
    finish_reason: str | None = None,
    error_detail: str | None = None,
    validation_errors: list[dict] | None = None,
) -> S1AiJsonResult:
    diagnostics = {}
    if error_code == 'duplicate_json_key':
        diagnostics = {
            'contract': 'json_object', 'repairs': [],
            'issues': [{
                'path': str(error_detail or '').removeprefix('duplicate_key:'),
                'reason': 'duplicate_json_key', 'rule': 'unique_json_keys',
            }],
        }
    return S1AiJsonResult(
        content=content,
        error_code=error_code,
        error_detail=error_detail,
        raw_excerpt=build_s1_ai_raw_excerpt(content),
        finish_reason=finish_reason,
        validation_errors=list(validation_errors or []),
        field_name_diagnostics=diagnostics,
    )


def _truncated_output_result(extracted: S1AiJsonResult) -> S1AiJsonResult:
    """截断正文只能留下有界诊断摘要，绝不进入任何后续重试上下文。"""
    return S1AiJsonResult(
        content='',
        error_code='truncated_output',
        error_detail='finish_reason=length',
        raw_excerpt=build_s1_ai_raw_excerpt(extracted.content),
        finish_reason=extracted.finish_reason,
        repair_applied=extracted.repair_applied,
    )


def extract_s1_ai_response_content(response_json: object) -> S1AiJsonResult:
    """安全提取 OpenAI-compatible 响应正文，不直接索引 ``choices[0]``。"""
    if not isinstance(response_json, dict):
        return _error_result('invalid_response_envelope', error_detail='response_not_object')
    choices = response_json.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return _error_result('missing_choices')
    choice = choices[0]
    finish_reason = choice.get('finish_reason')
    finish_reason = str(finish_reason) if finish_reason is not None else None
    message = choice.get('message')
    if not isinstance(message, dict):
        return _error_result('missing_message', finish_reason=finish_reason)
    raw_content = message.get('content')
    if raw_content is not None and not isinstance(raw_content, str):
        return _error_result(
            'invalid_content_type',
            finish_reason=finish_reason,
            error_detail=type(raw_content).__name__,
        )
    content = raw_content.strip() if isinstance(raw_content, str) else ''
    if finish_reason == 'content_filter' or extract_s1_ai_structured_refusal(response_json):
        return _error_result('provider_refusal', content=content, finish_reason=finish_reason)
    if not content:
        return _error_result('empty_content', content='', finish_reason=finish_reason)
    extracted = S1AiJsonResult(
        content=content,
        raw_excerpt=build_s1_ai_raw_excerpt(content),
        finish_reason=finish_reason,
    )
    if finish_reason == 'length':
        return _truncated_output_result(extracted)
    return extracted


def _response_text(response: Any) -> str:
    raw_text = getattr(response, 'text', '')
    if isinstance(raw_text, str):
        return raw_text.strip()
    if isinstance(raw_text, bytes):
        return raw_text.decode('utf-8', errors='replace').strip()
    return ''


def _response_runtime_metadata(response: Any) -> dict[str, Any]:
    raw_metadata = getattr(response, '_s1_ai_runtime_metadata', None)
    metadata = {
        str(key): str(value)
        for key, value in (raw_metadata.items() if isinstance(raw_metadata, dict) else ())
        if value is not None
        and key != S1_AI_FAILURE_EVIDENCE_KEY
    }
    evidence = (get_s1_ai_failure_evidence(response)
                or get_s1_ai_failure_evidence(raw_metadata))
    if evidence is not None:
        metadata[S1_AI_FAILURE_EVIDENCE_KEY] = evidence
    return metadata


def _response_failover_usages(response: Any) -> list[dict]:
    raw_usages = getattr(response, '_s1_ai_failover_usages', None)
    if not isinstance(raw_usages, list):
        return []
    return [dict(usage) for usage in raw_usages if isinstance(usage, dict)]


def _response_attempt_receipts(response: Any) -> list[dict]:
    raw_receipts = getattr(response, '_s1_ai_attempt_receipts', None)
    if isinstance(raw_receipts, list):
        return [
            dict(receipt)
            for receipt in raw_receipts
            if isinstance(receipt, dict)
        ]
    # 兼容同进程中的旧包装器：保留逐次 usage，而不是继续合并成最终一行。
    return [
        {
            'usage': dict(usage),
            'failure_stage': 'provider_failover',
            'validation_error_code': 'legacy_failover_usage',
        }
        for usage in _response_failover_usages(response)
    ]


def merge_s1_ai_attempt_receipts_into_usage(
    usage: dict | None,
    *,
    request_metadata: dict | None = None,
    request_exception: BaseException | None = None,
) -> dict | None:
    """把网关失败回执并入记账载体；不合并各次 provider token 数。"""
    normalized = dict(usage) if isinstance(usage, dict) else {}
    existing = normalized.get(S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY)
    receipt_candidates = [
        existing,
        (
            request_metadata.get(S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY)
            if isinstance(request_metadata, dict)
            else None
        ),
        getattr(request_exception, '_s1_ai_attempt_receipts', None),
    ]
    receipts = next(
        (
            [
                dict(receipt)
                for receipt in candidate[:12]
                if isinstance(receipt, dict)
            ]
            for candidate in receipt_candidates
            if isinstance(candidate, list) and candidate
        ),
        [],
    )
    if receipts:
        normalized[S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY] = receipts
    evidence = (get_s1_ai_failure_evidence(normalized)
                or get_s1_ai_failure_evidence(request_metadata))
    if evidence is not None:
        normalized[S1_AI_FAILURE_EVIDENCE_KEY] = evidence
    return normalized or None


def _response_usage(
    response_json: object,
    *,
    attempt_receipts: list[dict] | None = None,
    runtime_metadata: dict | None = None,
) -> dict | None:
    usage = (
        response_json.get('usage')
        if isinstance(response_json, dict)
        else None
    )
    normalized = dict(usage) if isinstance(usage, dict) else {}
    # 项目保留键只接受本层从真实响应与网关回执计算出的值，避免把 provider
    # 自报的同名扩展误当成精确统计或路由审计。
    normalized.pop(S1_AI_VISIBLE_REASONING_CHARS_KEY, None)
    normalized.pop(S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY, None)
    normalized.pop(S1_AI_ROUTE_METADATA_KEY, None)
    normalized.pop(S1_AI_FAILURE_EVIDENCE_KEY, None)
    evidence = get_s1_ai_failure_evidence(runtime_metadata)
    if evidence is not None:
        normalized[S1_AI_FAILURE_EVIDENCE_KEY] = evidence
    visible_reasoning_chars = extract_s1_ai_visible_reasoning_chars(response_json)
    if visible_reasoning_chars is not None:
        normalized[S1_AI_VISIBLE_REASONING_CHARS_KEY] = visible_reasoning_chars
    if attempt_receipts:
        normalized[S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY] = [
            dict(receipt)
            for receipt in attempt_receipts
            if isinstance(receipt, dict)
        ]
    route_metadata = {
        field: runtime_metadata[field]
        for field in S1_AI_ROUTE_METADATA_FIELDS
        if isinstance(runtime_metadata, dict)
        and runtime_metadata.get(field) not in (None, '')
    }
    if normalized and route_metadata:
        normalized[S1_AI_ROUTE_METADATA_KEY] = route_metadata
    return normalized or None


def _direct_body_result(
    content: str,
    *,
    repair_applied: bool,
    raw_excerpt: str,
    runtime_metadata: dict[str, str],
    response_json: dict | None = None,
    attempt_receipts: list[dict] | None = None,
) -> S1AiHttpResponseResult:
    candidate = str(content or '').strip()
    if not candidate:
        error = _error_result('empty_response_body')
        return S1AiHttpResponseResult(
            response_json=response_json,
            content_result=error,
            usage=_response_usage(
                response_json,
                attempt_receipts=attempt_receipts,
                runtime_metadata=runtime_metadata,
            ),
            recovery_mode='failed',
            raw_excerpt=raw_excerpt,
            runtime_metadata=runtime_metadata,
        )
    return S1AiHttpResponseResult(
        response_json=response_json,
        content_result=S1AiJsonResult(
            content=candidate,
            raw_excerpt=build_s1_ai_raw_excerpt(candidate),
            repair_applied=repair_applied,
        ),
        usage=_response_usage(
            response_json,
            attempt_receipts=attempt_receipts,
            runtime_metadata=runtime_metadata,
        ),
        recovery_mode='direct_body_candidate',
        raw_excerpt=raw_excerpt,
        runtime_metadata=runtime_metadata,
    )


def decode_s1_ai_http_response(
    response: Any,
    *,
    envelope_repair_mode: S1AiJsonRepairMode = 'lossless_syntax_only',
    check_http_status: bool = True,
) -> S1AiHttpResponseResult:
    """统一解码一次 S1 AI HTTP 响应，不在此层重试或判定业务成功。

    默认先检查并原样抛出 HTTP 错误；仅允许已在外层完成同等状态检查与传输重试的
    包装器显式关闭重复检查。仅对 HTTP 成功但外层 JSON 解码失败的正文尝试
    无语义的对象语法修复；无法恢复标准信封时返回 direct-body 候选，必须由
    调用方原有 parser/validator/consumer 完整验收。
    """
    attempt_receipts = _response_attempt_receipts(response)
    if check_http_status:
        try:
            response.raise_for_status()
        except Exception as exc:
            if attempt_receipts:
                try:
                    setattr(
                        exc,
                        '_s1_ai_attempt_receipts',
                        [dict(receipt) for receipt in attempt_receipts],
                    )
                except (AttributeError, TypeError):
                    pass
            raise
    raw_text = _response_text(response)
    raw_excerpt = build_s1_ai_raw_excerpt(raw_text)
    runtime_metadata = _response_runtime_metadata(response)
    try:
        response_json = response.json()
        outer_repair_applied = False
    except (TypeError, ValueError):
        repaired = parse_s1_ai_json_content(
            raw_text,
            repair_mode=envelope_repair_mode,
        )
        if not repaired.ok:
            return _direct_body_result(
                raw_text,
                repair_applied=False,
                raw_excerpt=raw_excerpt,
                runtime_metadata=runtime_metadata,
                attempt_receipts=attempt_receipts,
            )
        response_json = repaired.data
        outer_repair_applied = repaired.repair_applied

    if not isinstance(response_json, dict):
        error = _error_result(
            'invalid_response_envelope',
            content=raw_text,
            error_detail='response_not_object',
        )
        return S1AiHttpResponseResult(
            response_json=None,
            content_result=error,
            usage=_response_usage(
                None,
                attempt_receipts=attempt_receipts,
                runtime_metadata=runtime_metadata,
            ),
            recovery_mode='failed',
            raw_excerpt=raw_excerpt,
            runtime_metadata=runtime_metadata,
        )

    if 'choices' not in response_json:
        direct_content = raw_text or json.dumps(response_json, ensure_ascii=False)
        if response_json.get('error') is not None:
            return S1AiHttpResponseResult(
                response_json=response_json,
                content_result=_error_result('provider_error_envelope', content=direct_content),
                usage=_response_usage(
                    response_json, attempt_receipts=attempt_receipts,
                    runtime_metadata=runtime_metadata,
                ),
                recovery_mode='failed', raw_excerpt=raw_excerpt,
                runtime_metadata=runtime_metadata,
            )
        return _direct_body_result(
            direct_content,
            repair_applied=outer_repair_applied,
            raw_excerpt=raw_excerpt,
            runtime_metadata=runtime_metadata,
            response_json=response_json,
            attempt_receipts=attempt_receipts,
        )

    extracted = extract_s1_ai_response_content(response_json)
    if outer_repair_applied:
        extracted = replace(extracted, repair_applied=True)
    recovery_mode: S1AiHttpRecoveryMode = (
        'repaired_envelope' if outer_repair_applied else 'strict_envelope'
    )
    if extracted.error_code:
        recovery_mode = 'failed'
    return S1AiHttpResponseResult(
        response_json=response_json,
        content_result=extracted,
        usage=_response_usage(
            response_json,
            attempt_receipts=attempt_receipts,
            runtime_metadata=runtime_metadata,
        ),
        recovery_mode=recovery_mode,
        raw_excerpt=raw_excerpt,
        runtime_metadata=runtime_metadata,
    )


def _load_json_without_duplicate_keys(
    text: str, *, allow_identical: bool = False,
    coalesced_paths: list[str] | None = None,
) -> object:
    class ObjectPairs(list):
        pass

    def materialize(value, path):
        if isinstance(value, ObjectPairs):
            result = {}
            for key, child in value:
                child_path = f'{path}.{key}'
                child = materialize(child, child_path)
                if key in result:
                    # JSON fingerprints preserve primitive types and array order;
                    # Python equality alone would conflate true with 1.
                    identical = False
                    if allow_identical:
                        try:
                            identical = json.dumps(
                                result[key], sort_keys=True, allow_nan=False,
                            ) == json.dumps(child, sort_keys=True, allow_nan=False)
                        except (TypeError, ValueError):
                            # Non-finite values have no lossless JSON fingerprint.
                            # Keep this a duplicate conflict so generic syntax
                            # repair cannot discard one of the declarations.
                            pass
                    if not identical:
                        raise _DuplicateJsonKeyError(f'duplicate_key:{child_path}')
                    if coalesced_paths is not None:
                        coalesced_paths.append(child_path)
                    continue
                result[key] = child
            return result
        if isinstance(value, list):
            return [materialize(child, f'{path}[{index}]') for index, child in enumerate(value)]
        return value

    return materialize(json.loads(text, object_pairs_hook=ObjectPairs), '$')


def _unwrap_exact_json_fence(text: str) -> str | None:
    candidate = text.strip()
    if candidate.startswith('```'):
        if not candidate.endswith('```'):
            return None
        first_newline = candidate.find('\n')
        if first_newline < 0:
            return None
        opener = candidate[:first_newline].strip().lower()
        if opener not in {'```', '```json'}:
            return None
        candidate = candidate[first_newline + 1:-3].strip()
    return candidate


def repair_s1_ai_json_brackets(
    text: str,
    *,
    candidate_projector: Callable[[dict], object] | None = None,
) -> tuple[str | None, bool]:
    """供所有 S1 AI 共用的有界单括号修复，不内置任何角色业务字段。

    默认要求完整 JSON 对象唯一；角色可提供无副作用的合同投影来排除非法候选、
    比较实际消费结果。第二个返回值表示已进入修复范围；搜索不足或结果有歧义时
    调用者不能再用启发式修复任选结构。只增删一个字符串外的括号；同键同类型同值
    的重复声明可无损折叠，冲突值仍拒绝，不能靠后值覆盖前值。
    """
    candidate = _unwrap_exact_json_fence(text)
    if candidate is None:
        return None, False
    text = candidate
    marks: list[tuple[int, str]] = []
    gaps: list[int] = []
    in_string = escaped = False
    counts = dict.fromkeys('{}[]', 0)
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if not char.isspace():
            gaps.append(index)
        if char == '"':
            in_string = True
        elif char in counts:
            marks.append((index, char))
            counts[char] += 1
    if in_string:
        return None, False
    balances = (counts['{'] - counts['}'], counts['['] - counts[']'])
    if sum(abs(balance) for balance in balances) != 1:
        return None, False
    opening, closing = ('{', '}') if balances[0] else ('[', ']')
    balance = balances[0] or balances[1]
    remove, insert = (opening, closing) if balance > 0 else (closing, opening)
    deletions = [index for index, char in marks if char == remove]
    gaps.append(len(text))

    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    limits = []
    for suffix in ('MAX_CHARS', 'MAX_CANDIDATES'):
        key = f'ADVENTURER_S1_AI_JSON_BRACKET_REPAIR_{suffix}'
        default = AdventurerS1Plugin.default_config[key]
        value = current_app.config.get(key, default) if has_app_context() else default
        limit, _ = normalize_ascii_integer(value, minimum=0)
        limits.append(limit if type(limit) is int else default)
    if len(text) > limits[0] or len(deletions) + len(gaps) > limits[1]:
        return None, True

    edits = [(index, '', 1) for index in deletions]
    edits.extend((index, insert, 0) for index in gaps)
    accepted_text = accepted_fingerprint = None
    saw_syntax_candidate = False
    for index, replacement, width in edits:
        candidate_text = text[:index] + replacement + text[index + width:]
        try:
            coalesced_paths: list[str] = []
            parsed = _load_json_without_duplicate_keys(
                candidate_text, allow_identical=True,
                coalesced_paths=coalesced_paths,
            )
            saw_syntax_candidate = True
            if not isinstance(parsed, dict):
                continue
            fingerprint = json.dumps(
                parsed, ensure_ascii=False, sort_keys=True, allow_nan=False,
            )
            if candidate_projector is not None:
                projected = candidate_projector(parsed)
                if projected is None:
                    continue
                fingerprint = json.dumps(
                    projected, ensure_ascii=False, sort_keys=True, allow_nan=False,
                )
        except _DuplicateJsonKeyError:
            # 完整候选不能靠覆盖重复键通过；不要再交给通用修复器丢掉其中一项。
            saw_syntax_candidate = True
            continue
        except (TypeError, ValueError, RecursionError):
            continue
        if accepted_fingerprint is not None and fingerprint != accepted_fingerprint:
            return None, True
        accepted_text = (
            json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
            if coalesced_paths else candidate_text
        )
        accepted_fingerprint = fingerprint
    # 连一个严格语法候选都没有时，仍允许原有引号/逗号等兼容流程处理。
    return accepted_text, saw_syntax_candidate


def _extract_exact_json_object_candidate(text: str) -> str | None:
    """Unwrap an exact JSON fence, then accept exactly one top-level object."""
    candidate = _unwrap_exact_json_fence(text)
    if candidate is None:
        return None
    if not candidate.startswith('{'):
        return None

    candidates: list[str] = []
    stack: list[str] = []
    start: int | None = None
    in_string = False
    escaped = False

    for index, char in enumerate(candidate):
        if start is None:
            if char == '{':
                start = index
                stack.append('}')
            elif not char.isspace():
                return None
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == '{':
            stack.append('}')
        elif char == '[':
            stack.append(']')
        elif char in '}]':
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack:
                candidates.append(candidate[start:index + 1])
                start = None
                if len(candidates) > 1:
                    return None

    if start is not None:
        if candidates:
            return None
        return candidate[start:]
    return candidates[0] if len(candidates) == 1 else None


def _container_stack_before(text: str, stop: int) -> list[str] | None:
    stack: list[str] = []
    in_string = False
    escaped = False

    for char in text[:stop]:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            expected = '{' if char == '}' else '['
            if not stack or stack[-1] != expected:
                return None
            stack.pop()

    return None if in_string else stack


def _insert_missing_object_member_comma(
    text: str,
    error: json.JSONDecodeError,
) -> str | None:
    """Insert a comma only when the next token is an object member key."""
    if error.msg != "Expecting ',' delimiter":
        return None

    next_index = error.pos
    while next_index < len(text) and text[next_index].isspace():
        next_index += 1
    if next_index >= len(text) or text[next_index] != '"':
        return None

    stack = _container_stack_before(text, next_index)
    if not stack or stack[-1] != '{':
        return None

    previous_index = next_index - 1
    while previous_index >= 0 and text[previous_index].isspace():
        previous_index -= 1
    if (
        previous_index < 0
        or text[previous_index] in ('{', '[', ',', ':')
    ):
        return None

    try:
        key, consumed = json.JSONDecoder().raw_decode(text[next_index:])
    except json.JSONDecodeError:
        return None
    if not isinstance(key, str):
        return None
    colon_index = next_index + consumed
    while colon_index < len(text) and text[colon_index].isspace():
        colon_index += 1
    if colon_index >= len(text) or text[colon_index] != ':':
        return None
    return text[:next_index] + ',' + text[next_index:]


def _remove_trailing_json_commas(text: str) -> tuple[str, bool]:
    chars: list[str] = []
    in_string = False
    escaped = False
    changed = False
    length = len(text)

    for index, char in enumerate(text):
        if in_string:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            chars.append(char)
            continue
        if char == ',':
            next_index = index + 1
            while next_index < length and text[next_index].isspace():
                next_index += 1
            if next_index < length and text[next_index] in '}]':
                changed = True
                continue
        chars.append(char)

    return ''.join(chars), changed


def _load_with_missing_object_member_commas(
    text: str,
) -> tuple[object | None, str, json.JSONDecodeError | None]:
    current = text
    while True:
        try:
            return _load_json_without_duplicate_keys(
                current, allow_identical=True,
            ), current, None
        except _DuplicateJsonKeyError:
            raise
        except json.JSONDecodeError as error:
            repaired = _insert_missing_object_member_comma(current, error)
            if repaired is None:
                return None, current, error
            current = repaired


def _repair_json_losslessly(text: str) -> object:
    """Apply only uniquely reversible JSON punctuation repairs, then parse strictly."""
    candidate = _extract_exact_json_object_candidate(text)
    if candidate is None:
        raise ValueError('single_json_object_not_found')
    candidate, _ = _remove_trailing_json_commas(candidate)

    parsed, _, strict_error = _load_with_missing_object_member_commas(
        candidate,
    )
    if strict_error is None:
        return parsed
    raise strict_error


def parse_s1_ai_json_content(
    content: str | None,
    *,
    schema: type[BaseModel] | None = None,
    field_schema: dict | None = None,
    field_contract: str = 'caller',
    bracket_candidate_projector: Callable[[dict], object] | None = None,
    repair_mode: S1AiJsonRepairMode = 'syntax_only',
    finish_reason: str | None = None,
) -> S1AiJsonResult:
    """严格解析 JSON 对象，必要时仅修复语法，再可选执行 Pydantic 校验。"""
    text = str(content or '').strip()
    if not text:
        return _error_result('empty_content', finish_reason=finish_reason)

    effective_field_schema = field_schema if field_schema is not None else (
        schema.model_json_schema() if schema is not None else None
    )
    missing = object()
    parsed: object = missing
    strict_error: Exception | None = None
    repair_applied = False
    key_integrity_verified = False
    try:
        coalesced_paths: list[str] = []
        parsed = _load_json_without_duplicate_keys(
            text, allow_identical=repair_mode != 'none',
            coalesced_paths=coalesced_paths,
        )
        repair_applied = bool(coalesced_paths)
        key_integrity_verified = True
    except _DuplicateJsonKeyError as exc:
        return _error_result('duplicate_json_key', content=text,
                             finish_reason=finish_reason, error_detail=str(exc))
    except (TypeError, ValueError) as exc:
        strict_error = exc

    if parsed is missing and repair_mode != 'none':
        if finish_reason == 'length':
            return _error_result('truncated_output', content=text, finish_reason=finish_reason)
        def project_syntax_candidate(value):
            # Bracket candidates have already passed duplicate-safe JSON decoding.
            # Their aliases must survive the owner's projection used to compare them.
            compatible = normalize_ai_field_names(value, effective_field_schema)
            if compatible.issues:
                return None
            projected = (schema.model_validate(compatible.data).model_dump(mode='json')
                         if schema is not None else compatible.data)
            return (bracket_candidate_projector(projected)
                    if bracket_candidate_projector is not None else projected)

        projector = project_syntax_candidate if (schema is not None or bracket_candidate_projector is not None) else None
        fixed, bracket_repair_handled = repair_s1_ai_json_brackets(
            text, candidate_projector=projector,
        )
        if bracket_repair_handled:
            if fixed is None and schema is not None:
                # 唯一语法结果即使字段不合法，也交给下方 schema 保留精确字段错误。
                fixed, _ = repair_s1_ai_json_brackets(text)
            if fixed is None:
                return _error_result(
                    'repair_failed', content=text, finish_reason=finish_reason,
                    error_detail='single_bracket_repair_not_unique_or_unavailable',
                )
            parsed = _load_json_without_duplicate_keys(fixed)
            repair_applied = True
            key_integrity_verified = True

    if parsed is missing and repair_mode == 'lossless_syntax_only':
        try:
            parsed = _repair_json_losslessly(text)
            repair_applied = True
            key_integrity_verified = True
        except _DuplicateJsonKeyError as exc:
            return _error_result('duplicate_json_key', content=text,
                                 finish_reason=finish_reason, error_detail=str(exc))
        except Exception as exc:
            return _error_result(
                'repair_failed',
                content=text,
                finish_reason=finish_reason,
                error_detail=f'{strict_error}; lossless_repair={exc}'[:500],
            )

    if parsed is missing and repair_mode == 'syntax_only':
        start = text.find('{')
        end = text.rfind('}')
        candidate = text[start:end + 1] if start >= 0 and end > start else (
            text[start:] if start >= 0 else text
        )
        try:
            # Catch duplicates in fenced/comma-repaired JSON before the legacy
            # repairer can discard them. Other syntax keeps its existing owner policy.
            try:
                parsed = _repair_json_losslessly(text)
                key_integrity_verified = True
            except _DuplicateJsonKeyError as exc:
                return _error_result('duplicate_json_key', content=text,
                                     finish_reason=finish_reason, error_detail=str(exc))
            except (TypeError, ValueError):
                parsed = json_repair.loads(candidate)
            repair_applied = True
        except Exception as exc:
            return _error_result(
                'repair_failed',
                content=text,
                finish_reason=finish_reason,
                error_detail=f'{strict_error}; json_repair={exc}'[:500],
            )

    if parsed is missing:
        return _error_result(
            'invalid_json',
            content=text,
            finish_reason=finish_reason,
            error_detail=str(strict_error or 'json_parse_failed')[:500],
        )
    if not isinstance(parsed, dict):
        return _error_result(
            'not_object',
            content=text,
            finish_reason=finish_reason,
            error_detail=type(parsed).__name__,
        )

    result = S1AiJsonResult(
        data=parsed, content=text, raw_excerpt=build_s1_ai_raw_excerpt(text),
        finish_reason=finish_reason, repair_applied=repair_applied,
        key_integrity_verified=key_integrity_verified,
    )
    if effective_field_schema is not None:
        result = apply_s1_ai_field_schema(
            result, field_schema=effective_field_schema,
            contract=(f'{schema.__module__}.{schema.__qualname__}'
                      if schema is not None else field_contract),
        )
        if not result.ok:
            return result
        parsed = result.data
    if schema is not None:
        try:
            parsed = schema.model_validate(parsed).model_dump()
        except ValidationError as exc:
            return replace(
                result, data=None,
                error_code='schema_validation_failed',
                validation_errors=exc.errors(include_url=False),
            )
    return replace(result, data=parsed)


def parse_s1_ai_json_response(
    response_json: object,
    *,
    schema: type[BaseModel] | None = None,
    field_schema: dict | None = None,
    field_contract: str = 'caller',
    bracket_candidate_projector: Callable[[dict], object] | None = None,
    repair_mode: S1AiJsonRepairMode = 'syntax_only',
) -> S1AiJsonResult:
    """从供应商响应提取正文并解析为 JSON 对象。"""
    extracted = extract_s1_ai_response_content(response_json)
    if extracted.error_code:
        return extracted
    return parse_s1_ai_json_content(
        extracted.content,
        schema=schema,
        field_schema=field_schema,
        field_contract=field_contract,
        bracket_candidate_projector=bracket_candidate_projector,
        repair_mode=repair_mode,
        finish_reason=extracted.finish_reason,
    )


def parse_s1_ai_http_json_response(
    response: Any,
    *,
    schema: type[BaseModel] | None = None,
    field_schema: dict | None = None,
    field_contract: str = 'caller',
    bracket_candidate_projector: Callable[[dict], object] | None = None,
    repair_mode: S1AiJsonRepairMode = 'syntax_only',
    envelope_repair_mode: S1AiJsonRepairMode = 'lossless_syntax_only',
    check_http_status: bool = True,
) -> S1AiHttpJsonResponseResult:
    """统一解码 HTTP 响应，再按调用方声明的 JSON/schema 合同解析正文。"""
    http_result = decode_s1_ai_http_response(
        response,
        envelope_repair_mode=envelope_repair_mode,
        check_http_status=check_http_status,
    )
    extracted = http_result.content_result
    if extracted.error_code:
        return S1AiHttpJsonResponseResult(
            http_result=http_result,
            json_result=extracted,
        )

    parsed = parse_s1_ai_json_content(
        extracted.content,
        schema=schema,
        field_schema=field_schema,
        field_contract=field_contract,
        bracket_candidate_projector=bracket_candidate_projector,
        repair_mode=repair_mode,
        finish_reason=extracted.finish_reason,
    )
    if extracted.repair_applied and not parsed.repair_applied:
        parsed = replace(parsed, repair_applied=True)
    evidence = get_s1_ai_failure_evidence(http_result.usage)
    if evidence is not None:
        parsed = replace(parsed, evidence=evidence)
        if parsed.field_name_diagnostics:
            evidence.record_field_name_compatibility(parsed.field_name_diagnostics)
    return S1AiHttpJsonResponseResult(
        http_result=http_result,
        json_result=parsed,
    )
