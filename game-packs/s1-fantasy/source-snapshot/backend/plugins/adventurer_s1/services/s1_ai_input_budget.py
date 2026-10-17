"""One complete model-input budget, enforced only at the S1 request gateway."""
import json

import requests
from flask import current_app

from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer


class S1AiInputBudgetError(requests.RequestException):
    """A local, non-retryable rejection; no model response or physical attempt."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__(f'{code}: {detail}')


def enforce_s1_ai_input_budget(payload):
    """Count complete final model-facing JSON, including structured contracts."""
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_AI_INPUT_MAX_CHARS'
    maximum, _ = normalize_ascii_integer(
        current_app.config.get(key, AdventurerS1Plugin.default_config[key]), minimum=1,
    )
    if type(maximum) is not int or maximum < 1:
        raise S1AiInputBudgetError('s1_ai_input_budget_invalid', 'S1 AI 总输入预算必须为正整数')
    # Request routing, sampling and credentials are not model input. Count all
    # messages (including assistant/tool history), Anthropic system, tool schema
    # and structured output constraints after the gateway finishes assembly.
    model_input = {key: payload[key] for key in (
        'messages', 'system', 'tools', 'tool_choice', 'response_format',
        'functions', 'function_call', 'output_config',
    ) if key in payload}
    size = len(json.dumps(model_input, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    if size > maximum:
        raise S1AiInputBudgetError(
            's1_ai_input_budget_exceeded',
            f'chars={size} limit={maximum}；完整输入未截断，本次请求未发送',
        )
    return size
