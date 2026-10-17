"""Two autonomous world-change responsibilities, using the existing world route."""
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_input_budget import S1AiInputBudgetError
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    resolve_ai_profile, get_s1_ai_response_metadata,
)
from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    parse_s1_ai_http_json_response, S1AiResponseContractResult,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.services.s1_campaign_evolution_contract import (
    normalize_proposal, normalize_review, input_messages,
)


def call(snapshot, proposal=None, *, attempt=1):
    capability = 'campaign_evolution_review' if proposal is not None else 'campaign_evolution_plan'
    profile = resolve_ai_profile('world_outline')
    db_manager.remove()
    normalize = (lambda raw: normalize_review(raw, snapshot, proposal)) if proposal is not None else (
        lambda raw: normalize_proposal(raw, snapshot))
    def validate(decoded):
        if decoded.http_result.finish_reason == 'length':
            return S1AiResponseContractResult.reject('response_truncated', failure_stage='response_parse')
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json', failure_stage='response_parse')
        try:
            return S1AiResponseContractResult.accept(normalize(decoded.json_result.data))
        except ValueError:
            return S1AiResponseContractResult.reject('campaign_evolution_contract_invalid',
                                                     failure_stage='contract_validation')
    metadata, usage, result = {}, None, None
    error_code = None
    request_exception = None
    failure_stage = 'evolution_ai'
    try:
        messages = input_messages(snapshot, proposal)
        response = post_s1_ai_request_with_player_aliases(
            str(profile['api_base']).rstrip('/') + '/chat/completions',
            feature='world_outline', capability=capability,
            player_names=tuple(snapshot.get('player_names', [])),
            headers={'Authorization': 'Bearer ' + profile['api_key'], 'Content-Type': 'application/json'},
            json={'model': profile['model'], 'messages': messages},
            json_object_mode_if_supported=True, request_metadata_out=metadata,
            response_validator=validate,
        )
        decoded = parse_s1_ai_http_json_response(response)
        metadata.update(get_s1_ai_response_metadata(response))
        usage = decoded.http_result.usage
        if decoded.http_result.finish_reason == 'length':
            error_code = 'response_truncated'
            raise ValueError('战役定时演进 AI 响应被截断')
        if not decoded.json_result.ok:
            error_code = decoded.json_result.error_code or 'response_parse'
            raise ValueError('战役定时演进 AI 响应无法解析：' + error_code)
        result = normalize(decoded.json_result.data)
        return result
    except S1AiInputBudgetError as exc:
        request_exception = exc
        error_code, failure_stage = exc.code, 'input_budget'
        raise
    except Exception as exc:
        request_exception = exc
        error_code = error_code or type(exc).__name__
        raise
    finally:
        record_token_usage(call_type=capability + '_ai', usage=usage,
            model=metadata.get('model') or profile['model'], attempt_number=attempt,
            correlation_id='campaign:' + snapshot['input_hash'],
            is_success=result is not None, failure_stage=None if result is not None else failure_stage,
            validation_error_code=error_code, record_without_usage=True, request_metadata=metadata,
            request_exception=request_exception)
        db_manager.remove()
