"""Provider-specific HTTP payload and response normalization helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse


ANTHROPIC_COMPATIBLE_PROVIDER = "anthropic_compatible"
ANTHROPIC_VERSION = "2023-06-01"

# These are transport controls, not business data. Keep the list explicit so
# tool schemas or message bodies that happen to mention the same words remain
# untouched.
OUTPUT_TOKEN_LIMIT_KEYS = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "max_new_tokens",
        "max_response_tokens",
        "output_token_limit",
        "completion_token_limit",
    }
)
_OUTPUT_LIMIT_CONTAINER_KEYS = (
    "generation_config",
    "output_config",
    "completion_config",
)


def is_anthropic_compatible(profile: Mapping[str, Any]) -> bool:
    return (
        str(profile.get("provider") or "").strip().lower()
        == ANTHROPIC_COMPATIBLE_PROVIDER
    )


def strip_output_token_limits(payload: dict[str, Any]) -> None:
    """Mechanically remove output-token caps from provider request controls."""
    for key in OUTPUT_TOKEN_LIMIT_KEYS:
        payload.pop(key, None)
    for container_key in _OUTPUT_LIMIT_CONTAINER_KEYS:
        raw_container = payload.get(container_key)
        if not isinstance(raw_container, Mapping):
            continue
        container = dict(raw_container)
        for key in OUTPUT_TOKEN_LIMIT_KEYS:
            container.pop(key, None)
        if container:
            payload[container_key] = container
        else:
            payload.pop(container_key, None)


def anthropic_messages_url(api_base: str) -> str:
    """Build ``/v1/messages`` without duplicating a trailing ``/v1``."""
    parsed = urlparse(str(api_base or "").strip().rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/messages"):
        pass
    elif path.endswith("/v1"):
        path = f"{path}/messages"
    else:
        path = f"{path}/v1/messages"
    return urlunparse(parsed._replace(path=path))


def anthropic_request_headers(
    api_key: str,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Project caller headers onto the native Anthropic authentication scheme."""
    headers = {
        str(key): str(value)
        for key, value in (existing or {}).items()
    }
    for key in list(headers):
        if key.lower() in {
            "authorization",
            "x-api-key",
            "anthropic-version",
            "content-type",
        }:
            headers.pop(key, None)
    headers.update(
        {
            "x-api-key": str(api_key),
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
    )
    return headers


def _text_from_system_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Anthropic system content must be text or text blocks")
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise ValueError("Anthropic system content block must be an object")
        block_type = str(block.get("type") or "")
        text = block.get("text")
        if block_type not in {"text", "input_text", "output_text"} or not isinstance(
            text,
            str,
        ):
            raise ValueError("Anthropic system content only supports text blocks")
        parts.append(text)
    return "".join(parts)


def _anthropic_content_block(block: object) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        raise ValueError("Anthropic message content block must be an object")
    block_type = str(block.get("type") or "")
    if block_type in {"text", "input_text", "output_text"}:
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError("Anthropic text block must contain text")
        return {"type": "text", "text": text}
    if block_type == "image_url":
        raw_image = block.get("image_url")
        image_url = (
            raw_image.get("url")
            if isinstance(raw_image, Mapping)
            else raw_image
        )
        if not isinstance(image_url, str) or not image_url:
            raise ValueError("Anthropic image_url block must contain a URL")
        return {
            "type": "image",
            "source": {"type": "url", "url": image_url},
        }
    if block_type in {
        "text",
        "image",
        "document",
        "tool_use",
        "tool_result",
        "search_result",
    }:
        return dict(block)
    raise ValueError(f"unsupported Anthropic content block: {block_type or 'missing'}")


def _anthropic_message_content(content: object) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Anthropic message content must be text or content blocks")
    return [_anthropic_content_block(block) for block in content]


def _tool_use_blocks(raw_tool_calls: object) -> list[dict[str, Any]]:
    if raw_tool_calls is None:
        return []
    if not isinstance(raw_tool_calls, list):
        raise ValueError("OpenAI tool_calls must be an array")
    blocks: list[dict[str, Any]] = []
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, Mapping):
            raise ValueError("OpenAI tool_call must be an object")
        function = raw_call.get("function")
        if not isinstance(function, Mapping):
            raise ValueError("OpenAI tool_call.function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("OpenAI tool_call.function.name is required")
        call_id = str(raw_call.get("id") or "").strip()
        if not call_id:
            raise ValueError("OpenAI tool_call.id is required")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("OpenAI tool_call arguments must be JSON") from exc
        if not isinstance(arguments, Mapping):
            raise ValueError("OpenAI tool_call arguments must decode to an object")
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": dict(arguments),
            }
        )
    return blocks


def _append_content_blocks(
    content: str | list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    if not additions:
        return content
    if isinstance(content, str):
        blocks = ([{"type": "text", "text": content}] if content else [])
    else:
        blocks = list(content)
    blocks.extend(additions)
    return blocks


def _anthropic_messages(messages: object) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("Anthropic request messages must be a non-empty array")
    system_parts: list[str] = []
    projected: list[dict[str, Any]] = []
    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            raise ValueError("Anthropic request message must be an object")
        role = str(raw_message.get("role") or "").strip().lower()
        if role in {"system", "developer"}:
            system_parts.append(
                _text_from_system_content(raw_message.get("content"))
            )
            continue
        if role == "tool":
            tool_use_id = str(raw_message.get("tool_call_id") or "").strip()
            if not tool_use_id:
                raise ValueError("OpenAI tool message requires tool_call_id")
            projected.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": _anthropic_message_content(
                                raw_message.get("content", "")
                            ),
                        }
                    ],
                }
            )
            continue
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported Anthropic message role: {role or 'missing'}")
        raw_content = raw_message.get("content", "")
        if (
            role == "assistant"
            and raw_content is None
            and raw_message.get("tool_calls") is not None
        ):
            raw_content = ""
        content = _anthropic_message_content(raw_content)
        if role == "assistant":
            content = _append_content_blocks(
                content,
                _tool_use_blocks(raw_message.get("tool_calls")),
            )
        projected.append({"role": role, "content": content})
    if not projected:
        raise ValueError("Anthropic request requires a user or assistant message")
    return "\n\n".join(system_parts), projected


def _anthropic_tools(raw_tools: object) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        raise ValueError("Anthropic tools must be an array")
    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, Mapping):
            raise ValueError("Anthropic tool must be an object")
        if "input_schema" in raw_tool:
            tools.append(dict(raw_tool))
            continue
        function = raw_tool.get("function")
        if raw_tool.get("type") != "function" or not isinstance(
            function,
            Mapping,
        ):
            raise ValueError("Anthropic adapter only supports function tools")
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name or not isinstance(
            parameters,
            Mapping,
        ):
            raise ValueError("OpenAI function tool requires name and parameters")
        tool = {
            "name": name,
            "input_schema": dict(parameters),
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        tools.append(tool)
    return tools


def _anthropic_tool_choice(raw_choice: object) -> dict[str, Any]:
    if isinstance(raw_choice, str):
        choice_type = {
            "required": "any",
            "function": "any",
        }.get(raw_choice, raw_choice)
        if choice_type in {"auto", "any", "none"}:
            return {"type": choice_type}
        raise ValueError(f"unsupported Anthropic tool_choice: {raw_choice}")
    if not isinstance(raw_choice, Mapping):
        raise ValueError("Anthropic tool_choice must be a string or object")
    if raw_choice.get("type") == "function":
        function = raw_choice.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(name, str) or not name:
            raise ValueError("OpenAI function tool_choice requires a name")
        return {"type": "tool", "name": name}
    return dict(raw_choice)


def to_anthropic_messages_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project an OpenAI-style request onto supported Messages API semantics."""
    source = dict(payload)
    strip_output_token_limits(source)
    system, messages = _anthropic_messages(source.get("messages"))
    projected: dict[str, Any] = {
        "model": source.get("model"),
        "messages": messages,
    }
    if system:
        projected["system"] = system
    for key in (
        "temperature",
        "top_p",
        "top_k",
        "metadata",
        "service_tier",
    ):
        if key in source:
            projected[key] = source[key]
    if source.get("stream") is True:
        projected["stream"] = True
    if "stop_sequences" in source:
        projected["stop_sequences"] = source["stop_sequences"]
    elif "stop" in source:
        stop = source["stop"]
        projected["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if "tools" in source:
        projected["tools"] = _anthropic_tools(source["tools"])
    if "tool_choice" in source:
        projected["tool_choice"] = _anthropic_tool_choice(
            source["tool_choice"]
        )
    strip_output_token_limits(projected)
    return projected


def anthropic_stop_reason_to_finish_reason(stop_reason: object) -> str | None:
    if stop_reason is None:
        return None
    normalized = str(stop_reason)
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "model_context_window_exceeded": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
        "pause_turn": "pause",
    }.get(normalized, normalized)


def normalize_anthropic_usage(raw_usage: object) -> dict[str, Any]:
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}

    def token_count(key: str) -> int:
        value = usage.get(key)
        return value if type(value) is int and value >= 0 else 0

    input_tokens = token_count("input_tokens")
    cache_creation = token_count("cache_creation_input_tokens")
    cache_read = token_count("cache_read_input_tokens")
    completion_tokens = token_count("output_tokens")
    if not any(
        type(usage.get(key)) is int
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    ):
        return {}
    prompt_tokens = input_tokens + cache_creation + cache_read
    normalized = {
        "prompt_tokens": prompt_tokens,
        "prompt_cache_hit_tokens": cache_read,
        "prompt_cache_miss_tokens": input_tokens + cache_creation,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cache_read or cache_creation:
        normalized["prompt_tokens_details"] = {
            "cached_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
        }
    return normalized


def normalize_anthropic_response(body: object) -> dict[str, Any]:
    """Return the OpenAI-compatible envelope consumed by existing callers."""
    if not isinstance(body, Mapping) or body.get("type") != "message":
        raise ValueError("Anthropic provider returned an invalid message envelope")
    raw_content = body.get("content")
    if not isinstance(raw_content, list):
        raise ValueError("Anthropic provider content must be an array")
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in raw_content:
        if not isinstance(block, Mapping):
            raise ValueError("Anthropic provider content block must be an object")
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Anthropic text block must contain text")
            text_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                reasoning_parts.append(thinking)
        elif block_type == "tool_use":
            name = block.get("name")
            tool_id = str(block.get("id") or "").strip()
            tool_input = block.get("input")
            if (
                not tool_id
                or not isinstance(name, str)
                or not isinstance(tool_input, Mapping)
            ):
                raise ValueError("Anthropic tool_use block is invalid")
            tool_calls.append(
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(
                            dict(tool_input),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            )

    stop_reason = body.get("stop_reason")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if stop_reason == "refusal":
        message["refusal"] = "".join(text_parts) or "provider_refusal"
    envelope: dict[str, Any] = {
        "id": str(body.get("id") or ""),
        "object": "chat.completion",
        "model": str(body.get("model") or ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": anthropic_stop_reason_to_finish_reason(
                    stop_reason
                ),
            }
        ],
    }
    normalized_usage = normalize_anthropic_usage(body.get("usage"))
    if normalized_usage:
        envelope["usage"] = normalized_usage
    return envelope
