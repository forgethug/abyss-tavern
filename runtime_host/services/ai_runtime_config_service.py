"""S1 model routing with private, independently persisted tavern overrides."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import threading
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlparse

from flask import current_app
from services.model_provider_transport import ANTHROPIC_COMPATIBLE_PROVIDER


class AIProfileAssignmentError(RuntimeError):
    """The pack requested an unknown or unavailable model route."""


STALE_AI_CONFIG_ERROR = "模型目录、当前用途分配或思考设置已更新，请刷新后重试"
STALE_AI_BALANCE_BLOCK_ERROR = "凭证或余额暂停状态已更新，请刷新模型目录后重试"
MISSING_AI_CONFIG_REVISION_ERROR = "缺少配置修订号，请刷新后重试"
STALE_AI_CONFIG_SYNC_TARGET_ERROR = "AI 配置已更新，请重新预览后再同步"
AI_CONFIG_SYNC_PREVIEW_MISMATCH_ERROR = "同步选择或预览快照已变化，请重新预览后再同步"


# Mirror the S1 feature catalogue, not the official server's config writer.
# S1 checks this exact catalogue against its declarations when it imports.
AI_FEATURES_BY_SCOPE = {"s1": (
    {"key": "expedition_main", "label": "探险主剧情生成"},
    {"key": "expedition_outline", "label": "探险客观局势大纲"},
    {"key": "expedition_resolution", "label": "探险高危主剧情因果底稿"},
    {"key": "expedition_death_resource_review", "label": "探险生命资源一致性复核"},
    {"key": "expedition_observer", "label": "探险剧情审核"},
    {"key": "expedition_countermeasure_learning", "label": "长期主体经验学习"},
    {"key": "expedition_observer_verdict", "label": "探险审核输出轻量裁决"},
    {"key": "expedition_content_safety_review", "label": "探险存量内容处置复核"},
    {"key": "settlement_guard", "label": "结算结构守卫"},
    {"key": "fate_historical_review", "label": "历史命途删除复核"},
    {"key": "companion_historical_review", "label": "历史伙伴连续性复核"},
    {"key": "tavern_activity_generation", "label": "酒馆活动剧情生成"},
    {"key": "tavern_activity_review", "label": "酒馆活动剧情审核"},
    {"key": "data_appeal", "label": "数据申诉初审"},
    {"key": "data_appeal_rarity", "label": "数据申诉稀有度复核"},
    {"key": "data_appeal_review", "label": "数据申诉二审"},
    {"key": "data_appeal_player_review", "label": "玩家复核与撤回审核"},
    {"key": "appeal_fact_sheet", "label": "申诉事实表整理"},
    {"key": "tavern_relief", "label": "酒馆补贴申请 AI 核定"},
    {"key": "building_ai", "label": "建筑功能 AI"},
    {"key": "building_healing_house", "label": "治疗馆疗愈结果"},
    {"key": "building_blacksmith", "label": "铁匠铺锻造结果"},
    {"key": "building_tailor_workshop", "label": "绮罗裁坊裁制结果"},
    {"key": "building_alchemy_workshop", "label": "炼金工坊制作结果"},
    {"key": "building_item_assembly", "label": "构装工坊组装结果"},
    {"key": "building_ember_kitchen", "label": "余烬灶坊烹饪结果"},
    {"key": "building_chapel", "label": "礼拜堂祈愿结果"},
    {"key": "building_pet_evolution", "label": "嬗变祭坛进化结果"},
    {"key": "building_skill_training", "label": "导师授艺结果"},
    {"key": "ambient_npc_gift", "label": "环境伙伴赠礼反应"},
    {"key": "commission_generation", "label": "委托生成"},
    {"key": "world_intel", "label": "世界情报生成"},
    {"key": "world_outline", "label": "世界大纲维护"},
    {"key": "world_actor_profile_backfill", "label": "世界角色能力画像回填"},
    {"key": "fate_journey", "label": "命途旅程生成"},
    {"key": "clue_bureau_boss", "label": "玄机阁首领生成"},
    {"key": "trait_recovery", "label": "特质恢复剧本"},
    {"key": "trait_description", "label": "特质描述压缩"},
    {"key": "trait_context_routing", "label": "短特质机制与分类"},
    {"key": "expedition_countermeasure_routing", "label": "探险反制知识上下文路由"},
    {"key": "description_compression", "label": "资产描述压缩"},
    {"key": "asset_description_update", "label": "既有资产描述修改"},
    {"key": "description_summary_sample_audit", "label": "描述摘要逐条激活审核"},
    {"key": "description_backfill", "label": "物品描述回填"},
)}


OPENROUTER_DEEPSEEK_V4_PRO_BAIDU_PROVIDER = "openrouter_deepseek_v4_pro_baidu"
OPENROUTER_DEEPSEEK_V4_PRO_OFFICIAL_PROVIDER = "openrouter_deepseek_v4_pro_official"
_OPENROUTER_MODEL = "deepseek/deepseek-v4-pro"
_EFFORTS = frozenset({"default", "none", "low", "medium", "high", "max"})
_CAPABILITIES = {
    "deepseek": frozenset({"json_object", "thinking", "tool_calls"}),
    OPENROUTER_DEEPSEEK_V4_PRO_BAIDU_PROVIDER: frozenset({"thinking"}),
    OPENROUTER_DEEPSEEK_V4_PRO_OFFICIAL_PROVIDER: frozenset(),
    "mimo": frozenset(),
    "openai_compatible": frozenset(),
    ANTHROPIC_COMPATIBLE_PROVIDER: frozenset({"tool_calls"}),
}


def _parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _parse_json(config.get("ADVENTURER_S1_AI_PROFILES_JSON"), [])
    profiles = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        profile_id = str(item.get("id") or "").strip()
        api_base = str(item.get("api_base") or "").strip().rstrip("/")
        model = str(item.get("model") or "").strip()
        if not profile_id or not api_base or not model:
            continue
        profiles.append(
            {
                **item,
                "id": profile_id,
                "name": str(item.get("name") or profile_id).strip(),
                "provider": str(item.get("provider") or "openai_compatible").strip(),
                "api_base": api_base,
                "model": model,
                "api_key": str(item.get("api_key") or "").strip(),
                "backup_api_key": str(item.get("backup_api_key") or "").strip(),
                "stream_enabled": item.get("stream_enabled") is True,
            }
        )
    if profiles:
        return profiles
    return [{
        "id": "default",
        "name": "default",
        "provider": str(config.get("ADVENTURER_S1_AI_PROVIDER") or "deepseek"),
        "api_base": str(config.get("DEEPSEEK_API_BASE") or "").rstrip("/"),
        "model": str(config.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"),
        "api_key": str(config.get("DEEPSEEK_API_KEY") or ""),
        "backup_api_key": "",
        "stream_enabled": False,
    }]


def _mapping(config: Mapping[str, Any], key: str) -> dict[str, str]:
    raw = _parse_json(config.get(key), {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def resolve_ai_profile(
    feature: str | None = None,
    *,
    scope: str,
    config: MutableMapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    if scope != "s1":
        raise ValueError("平台 Runtime 仅支持 S1 AI 配置域")
    from services.tavern_model_management import effective_config
    target = config if config is not None else effective_config()
    profiles = _profiles(target)
    by_id = {item["id"]: item for item in profiles}
    configured = str(target.get("ADVENTURER_S1_AI_DEFAULT_PROFILE_ID") or "")
    default_id = configured if configured in by_id else profiles[0]["id"]
    feature_key = str(feature or "")
    if feature_key and feature_key not in {
        item["key"] for item in AI_FEATURES_BY_SCOPE["s1"]
    }:
        raise AIProfileAssignmentError(f"未注册的 S1 AI 功能：{feature_key}")
    selected = _mapping(
        target, "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON"
    ).get(feature_key, default_id)
    if selected not in by_id:
        raise AIProfileAssignmentError(f"S1 AI 功能引用了不存在的配置：{feature_key}")
    resolved = dict(by_id[selected])
    backup_id = str(resolved.get("backup_profile_id") or "")
    if backup_id in by_id and backup_id != selected:
        resolved["backup_profile"] = dict(by_id[backup_id])
        resolved["backup_profile_resolution"] = "explicit"
    return resolved


def resolve_ai_request_config(
    feature: str | None = None,
    *,
    scope: str,
    config: MutableMapping[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], str]:
    from services.tavern_model_management import effective_config
    target = config if config is not None else effective_config()
    profile = resolve_ai_profile(feature, scope=scope, config=target, **kwargs)
    effort = _mapping(
        target, "ADVENTURER_S1_AI_FEATURE_REASONING_JSON"
    ).get(str(feature or ""), "default")
    return profile, effort if effort in _EFFORTS else "default"


def _is_openrouter_official(profile: Mapping[str, Any]) -> bool:
    parsed = urlparse(str(profile.get("api_base") or ""))
    return (
        str(profile.get("provider") or "")
        in {OPENROUTER_DEEPSEEK_V4_PRO_OFFICIAL_PROVIDER, "openai_compatible"}
        and parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "openrouter.ai"
        and parsed.path.rstrip("/") == "/api/v1"
        and str(profile.get("model") or "").strip().lower() == _OPENROUTER_MODEL
    )


def profile_capabilities(profile: Mapping[str, Any]) -> dict[str, bool]:
    provider = str(profile.get("provider") or "")
    supported = _CAPABILITIES.get(provider, frozenset())
    api_path = urlparse(str(profile.get("api_base") or "")).path.rstrip("/")
    json_mode = profile.get("json_object_mode", "auto")
    supports_json = "json_object" in supported or _is_openrouter_official(profile)
    if json_mode == "enabled" and provider != ANTHROPIC_COMPATIBLE_PROVIDER:
        supports_json = True
    elif json_mode == "disabled":
        supports_json = False
    return {
        "json_object": supports_json,
        "thinking": "thinking" in supported,
        "tool_calls": "tool_calls" in supported,
        "strict_tool_calls": provider == "deepseek" and api_path.endswith("/beta"),
    }


def profile_supports_json_object(profile: Mapping[str, Any]) -> bool:
    return profile_capabilities(profile)["json_object"]


def apply_provider_request_defaults(
    profile: Mapping[str, Any], payload: dict[str, Any]
) -> None:
    provider = str(profile.get("provider") or "")
    if _is_openrouter_official(profile):
        payload.pop("provider", None)
        return
    if provider == OPENROUTER_DEEPSEEK_V4_PRO_BAIDU_PROVIDER:
        payload["provider"] = {
            "only": ["baidu/fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        if "reasoning" not in payload:
            effort = payload.pop("reasoning_effort", None)
            if effort:
                payload["reasoning"] = {"effort": str(effort)}
            elif not (
                isinstance(payload.get("thinking"), Mapping)
                and payload["thinking"].get("type") == "disabled"
            ):
                payload["reasoning"] = {"effort": "high"}
        payload.pop("thinking", None)
        return
    if profile_capabilities(profile)["thinking"] and "thinking" not in payload:
        payload["thinking"] = {"type": "enabled"}


def apply_reasoning_effort_override(
    profile: Mapping[str, Any], payload: dict[str, Any], effort: str
) -> bool:
    normalized = "none" if effort == "disabled" else str(effort).lower()
    if normalized not in _EFFORTS:
        raise ValueError("不支持的 AI 思考设置")
    for key in (
        "thinking", "reasoning", "reasoning_effort", "include_reasoning",
        "thinking_budget", "thinking_level",
    ):
        payload.pop(key, None)
    output_config = payload.get("output_config")
    if isinstance(output_config, Mapping) and "effort" in output_config:
        remaining = dict(output_config)
        remaining.pop("effort", None)
        if remaining:
            payload["output_config"] = remaining
        else:
            payload.pop("output_config", None)
    if normalized == "default":
        return True
    provider = str(profile.get("provider") or "").strip().lower()
    if provider == ANTHROPIC_COMPATIBLE_PROVIDER:
        return False
    host = (urlparse(str(profile.get("api_base") or "")).hostname or "").lower()
    if provider.startswith("openrouter_") or host == "openrouter.ai":
        payload["reasoning"] = {"effort": normalized}
    elif provider == "deepseek" or host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        if normalized == "none":
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = (
                "high" if normalized in {"low", "medium"} else normalized
            )
    else:
        payload["reasoning_effort"] = "xhigh" if normalized == "max" else normalized
    return True


def get_ai_provider_config(*, scope: str) -> dict[str, Any]:
    if scope != "s1":
        raise ValueError("平台 Runtime 仅支持 S1 AI 配置域")
    from services.tavern_ai_config_store import read_catalog
    from services.tavern_model_management import catalog_config, revision_for
    stored, _ = read_catalog()
    config = catalog_config(current_app.config, stored)
    profiles = _profiles(config)
    by_id = {item["id"]: item for item in profiles}
    configured = str(config.get("ADVENTURER_S1_AI_DEFAULT_PROFILE_ID") or "")
    default_id = configured if configured in by_id else profiles[0]["id"]
    assignments = _mapping(config, "ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON")
    reasoning = _mapping(config, "ADVENTURER_S1_AI_FEATURE_REASONING_JSON")
    public_profiles = [{
        "id": item["id"], "name": item["name"], "provider": item["provider"],
        "api_base": item["api_base"], "model": item["model"],
        "stream_enabled": item["stream_enabled"],
        "api_key_configured": bool(item["api_key"]),
        "backup_api_key_configured": bool(item["backup_api_key"]),
        "balance_session_configured": bool(item.get("balance_session")),
        "backup_profile_id": str(item.get("backup_profile_id") or ""),
        "backup_api_base": str(item.get("backup_api_base") or ""),
        "backup_model": str(item.get("backup_model") or ""),
        "json_object_mode": str(item.get("json_object_mode") or "auto"),
        "backup_api_keys": [{
            "id": k.get("id", ""), "name": k.get("name", ""),
            "enabled": k.get("enabled") is not False,
            "api_key_configured": bool(k.get("api_key")),
            "balance_session_configured": bool(k.get("balance_session")),
        } for k in item.get("backup_api_keys", []) if isinstance(k, dict)],
        "is_default": item["id"] == default_id,
        "capabilities": profile_capabilities(item),
    } for item in profiles]
    revision = revision_for(config, stored)
    default_profile = by_id[default_id]
    return {
        "provider": default_profile["provider"],
        "api_base": default_profile["api_base"],
        "model": default_profile["model"],
        "api_key_configured": bool(default_profile["api_key"]),
        "profiles": public_profiles,
        "default_profile_id": default_id,
        "scope": "s1",
        "features": [{
            **feature, "scope": "s1",
            "profile_id": assignments.get(feature['key'], default_id),
            "reasoning_effort": reasoning.get(feature['key'], "default"),
        } for feature in AI_FEATURES_BY_SCOPE['s1']],
        "assignments": {f['key']: assignments.get(f['key'], default_id) for f in AI_FEATURES_BY_SCOPE['s1']},
        "reasoning_efforts": {f['key']: reasoning.get(f['key'], 'default') for f in AI_FEATURES_BY_SCOPE['s1']},
        "revision": revision,
        "shared_revision": revision,
        "managed_by": "tavern" if stored else "deployment_defaults",
        "runtime_id": current_app.config['GAME_RUNTIME_ID'],
        "blocked_credential_count": len(get_ai_credential_balance_blocks()),
        "balance_revision": _balance_revision(),
    }


def update_ai_provider_config(
    payload: dict[str, Any], *, scope: str
) -> tuple[dict | None, str | None]:
    if scope != 's1':
        return None, '仅支持 S1 配置'
    from services.tavern_model_management import update_catalog
    return update_catalog(payload)


def sync_ai_provider_config_from_public_source(*_args, **_kwargs):
    return None, "当前平台由中央控制面管理 AI 模型配置"


def refresh_ai_profile_balances(*_args, **_kwargs):
    return None, "请在中央控制面管理模型凭证和查询余额"


def resume_ai_credential_after_topup(payload, **_kwargs):
    if not isinstance(payload, dict) or payload.get('revision') != get_ai_provider_config(scope='s1')['revision']:
        return None, STALE_AI_CONFIG_ERROR
    with _balance_lock:
        if payload.get('balance_revision') != _balance_revision():
            return None, STALE_AI_BALANCE_BLOCK_ERROR
        current_app.config['ADVENTURER_S1_AI_CREDENTIAL_BALANCE_BLOCKS_JSON'] = {}
    return get_ai_provider_config(scope='s1'), None


def ai_credential_balance_identity(*, api_key="", balance_session=""):
    session = str(balance_session or "").strip()
    secret = session or str(api_key or "").strip()
    if not secret:
        return ""
    return ("account:" if session else "key:") + hashlib.sha256(
        secret.encode("utf-8")
    ).hexdigest()


_balance_lock = threading.RLock()


def _balance_revision():
    return hashlib.sha256(json.dumps(get_ai_credential_balance_blocks(),
        sort_keys=True).encode()).hexdigest()


def get_ai_credential_balance_blocks(config=None, **_kwargs):
    target = config if config is not None else current_app.config
    with _balance_lock:
        raw = _parse_json(target.get("ADVENTURER_S1_AI_CREDENTIAL_BALANCE_BLOCKS_JSON"), {})
        if not isinstance(raw, dict):
            return {}
        return {
            key: dict(value) for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, dict)
            and value.get("reason") == "insufficient_balance"
        }


def mark_ai_credential_balance_blocked(*, api_key="", balance_session="", source="unknown"):
    """Pause confirmed empty credentials within this runtime's process.

    The platform owns durable credential configuration. Return False honestly:
    this process-local cache is not a control-plane persistence receipt.
    """
    identity = ai_credential_balance_identity(api_key=api_key, balance_session=balance_session)
    if identity:
        with _balance_lock:
            blocks = get_ai_credential_balance_blocks()
            blocks[identity] = {
                "reason": "insufficient_balance",
                "blocked_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source)[:80],
            }
            current_app.config["ADVENTURER_S1_AI_CREDENTIAL_BALANCE_BLOCKS_JSON"] = blocks
    return False
