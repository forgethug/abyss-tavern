import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

import requests


class ModelGatewayError(RuntimeError):
    code = "model_gateway_error"


class ModelGatewayConfigurationError(ModelGatewayError):
    code = "model_gateway_configuration_error"


class ModelGatewayTransportError(ModelGatewayError):
    code = "model_gateway_transport_error"


class ModelGatewayResponseError(ModelGatewayError):
    code = "model_gateway_response_error"


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider: str
    api_base: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class ModelResponse:
    content: str
    finish_reason: Optional[str]
    usage: Mapping[str, Any]
    request_id: str
    profile_id: str
    provider: str
    model: str


def _parse_json(raw: str, expected_type: type, label: str):
    try:
        value = json.loads(raw or ("[]" if expected_type is list else "{}"))
    except json.JSONDecodeError as exc:
        raise ModelGatewayConfigurationError(
            f"{label} must be valid JSON"
        ) from exc
    if not isinstance(value, expected_type):
        raise ModelGatewayConfigurationError(
            f"{label} must be a JSON {expected_type.__name__}"
        )
    return value


class ModelGateway:
    """Shared, provider-neutral OpenAI-compatible model transport.

    Game packs declare capability names and request one of those capabilities.
    Provider credentials and capability-to-profile assignments stay in the
    platform/runtime configuration rather than in pack code.
    """

    def __init__(
        self,
        *,
        profiles: Iterable[ModelProfile],
        assignments: Mapping[str, str],
        declared_capabilities: Iterable[str],
        post: Callable[..., Any] = requests.post,
    ):
        self._profiles = {profile.profile_id: profile for profile in profiles}
        self._assignments = dict(assignments)
        self._declared_capabilities = frozenset(declared_capabilities)
        self._post = post

    @classmethod
    def from_environment(cls) -> "ModelGateway":
        raw_profiles = _parse_json(
            os.getenv("GAME_RUNTIME_MODEL_PROFILES_JSON", ""),
            list,
            "GAME_RUNTIME_MODEL_PROFILES_JSON",
        )
        profiles = []
        for item in raw_profiles:
            if not isinstance(item, dict):
                raise ModelGatewayConfigurationError(
                    "Every model profile must be an object"
                )
            profile_id = str(item.get("id") or "").strip()
            api_base = str(item.get("api_base") or "").strip().rstrip("/")
            model = str(item.get("model") or "").strip()
            if not profile_id or not api_base or not model:
                raise ModelGatewayConfigurationError(
                    "Model profiles require id, api_base and model"
                )
            try:
                timeout_seconds = float(item.get("timeout_seconds", 120))
            except (TypeError, ValueError) as exc:
                raise ModelGatewayConfigurationError(
                    f"Invalid timeout for model profile {profile_id}"
                ) from exc
            if timeout_seconds <= 0:
                raise ModelGatewayConfigurationError(
                    f"Invalid timeout for model profile {profile_id}"
                )
            profiles.append(
                ModelProfile(
                    profile_id=profile_id,
                    provider=str(
                        item.get("provider") or "openai_compatible"
                    ).strip(),
                    api_base=api_base,
                    api_key=str(item.get("api_key") or "").strip(),
                    model=model,
                    timeout_seconds=timeout_seconds,
                )
            )
        assignments = _parse_json(
            os.getenv("GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON", ""),
            dict,
            "GAME_RUNTIME_MODEL_ASSIGNMENTS_JSON",
        )
        capabilities = _parse_json(
            os.getenv("GAME_RUNTIME_MODEL_CAPABILITIES_JSON", ""),
            list,
            "GAME_RUNTIME_MODEL_CAPABILITIES_JSON",
        )
        if any(not isinstance(value, str) or not value for value in capabilities):
            raise ModelGatewayConfigurationError(
                "Model capabilities must be non-empty strings"
            )
        return cls(
            profiles=profiles,
            assignments={
                str(capability): str(profile_id)
                for capability, profile_id in assignments.items()
            },
            declared_capabilities=capabilities,
        )

    def status(self) -> dict:
        configured = sorted(
            capability
            for capability in self._declared_capabilities
            if self._assignments.get(capability) in self._profiles
        )
        return {
            "declared_capabilities": sorted(self._declared_capabilities),
            "configured_capabilities": configured,
        }

    def _resolve(self, capability: str) -> ModelProfile:
        if capability not in self._declared_capabilities:
            raise ModelGatewayConfigurationError(
                f"Game pack did not declare model capability: {capability}"
            )
        profile_id = self._assignments.get(capability)
        if not profile_id:
            raise ModelGatewayConfigurationError(
                f"No model profile assigned for capability: {capability}"
            )
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ModelGatewayConfigurationError(
                f"Assigned model profile is not configured: {profile_id}"
            )
        return profile

    def complete(
        self,
        capability: str,
        *,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_object: bool = False,
        timeout_seconds: Optional[float] = None,
        extra_payload: Optional[Mapping[str, Any]] = None,
    ) -> ModelResponse:
        profile = self._resolve(capability)
        payload = dict(extra_payload or {})
        payload.update({"model": profile.model, "messages": messages})
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if profile.api_key:
            headers["Authorization"] = f"Bearer {profile.api_key}"
        try:
            response = self.post_compatible(
                f"{profile.api_base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds or profile.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ModelGatewayTransportError(str(exc)) from exc
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelGatewayResponseError(
                "Model response has no assistant content"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelGatewayResponseError(
                "Model response assistant content is empty"
            )
        response_headers = getattr(response, "headers", {}) or {}
        return ModelResponse(
            content=content,
            finish_reason=choice.get("finish_reason"),
            usage=body.get("usage") if isinstance(body.get("usage"), dict) else {},
            request_id=str(
                response_headers.get("x-request-id")
                or response_headers.get("request-id")
                or ""
            ),
            profile_id=profile.profile_id,
            provider=profile.provider,
            model=profile.model,
        )

    def post_compatible(self, *args, **kwargs):
        """Return a raw requests-compatible response for legacy pack adapters."""
        return self._post(*args, **kwargs)


class RequestsTransportFacade:
    """Module-shaped adapter that only redirects HTTP POST to ModelGateway."""

    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    def post(self, *args, **kwargs):
        return self._gateway.post_compatible(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(requests, name)
