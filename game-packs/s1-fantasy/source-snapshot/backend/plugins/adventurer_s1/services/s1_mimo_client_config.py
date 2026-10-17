"""MIMO-compatible callers resolved exclusively through an S1 feature route."""
from flask import current_app


def get_mimo_compatible_client_config(
    feature: str,
) -> tuple[str | None, str, str, int]:
    """Return connection values from the mandatory route for ``feature``."""
    timeout = max(1, int(current_app.config.get('MIMO_TIMEOUT_SECONDS', 600)))
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
        resolve_ai_profile,
    )
    profile = resolve_ai_profile(feature)
    return (
        profile.get('api_key'),
        str(profile.get('api_base') or '').rstrip('/'),
        str(profile.get('model') or ''),
        timeout,
    )


def get_mimo_compatible_model_name(feature: str) -> str:
    return get_mimo_compatible_client_config(feature)[2]
