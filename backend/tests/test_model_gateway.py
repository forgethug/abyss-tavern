import json

import pytest

from runtime_core.model_gateway import (
    ModelGateway,
    ModelGatewayConfigurationError,
)


class _FakeResponse:
    headers = {"x-request-id": "request-1"}

    @staticmethod
    def raise_for_status():
        return None

    @staticmethod
    def json():
        return {
            "choices": [
                {
                    "message": {"content": "{\"ok\":true}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }


def test_gateway_resolves_declared_capability_without_exposing_secret(
    monkeypatch,
):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setenv(
        "GAME_RUNTIME_MODEL_PROFILES_JSON",
        json.dumps(
            [
                {
                    "id": "primary",
                    "provider": "openai_compatible",
                    "api_base": "https://models.example/v1/",
                    "api_key": "secret-value",
                    "model": "story-model",
                }
            ]
        ),
    )
    monkeypatch.setenv(
        "GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON",
        '{"narrative_generation":"primary"}',
    )
    monkeypatch.setenv(
        "GAME_RUNTIME_MODEL_CAPABILITIES_JSON",
        '["narrative_generation"]',
    )
    gateway = ModelGateway.from_environment()
    gateway._post = fake_post

    result = gateway.complete(
        "narrative_generation",
        messages=[{"role": "user", "content": "begin"}],
        json_object=True,
    )

    assert gateway.status() == {
        "declared_capabilities": ["narrative_generation"],
        "configured_capabilities": ["narrative_generation"],
    }
    assert "secret-value" not in repr(gateway.status())
    assert result.content == '{"ok":true}'
    assert result.request_id == "request-1"
    assert calls[0][0] == "https://models.example/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret-value"
    assert calls[0][1]["json"]["response_format"] == {
        "type": "json_object"
    }


def test_gateway_rejects_undeclared_capability(monkeypatch):
    monkeypatch.setenv("GAME_RUNTIME_MODEL_PROFILES_JSON", "[]")
    monkeypatch.setenv("GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON", "{}")
    monkeypatch.setenv("GAME_RUNTIME_MODEL_CAPABILITIES_JSON", "[]")

    gateway = ModelGateway.from_environment()

    with pytest.raises(
        ModelGatewayConfigurationError,
        match="did not declare",
    ):
        gateway.complete("hidden_capability", messages=[])
