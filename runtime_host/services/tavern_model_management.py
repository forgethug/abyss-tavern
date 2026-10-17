"""Private per-tavern model settings, independent of deployment defaults."""
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from flask import current_app

from services.tavern_ai_config_store import read_catalog, save_catalog

PROFILE_KEY = 'ADVENTURER_S1_AI_PROFILES_JSON'
ASSIGNMENT_KEY = 'ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON'
REASONING_KEY = 'ADVENTURER_S1_AI_FEATURE_REASONING_JSON'
DEFAULT_KEY = 'ADVENTURER_S1_AI_DEFAULT_PROFILE_ID'
ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def catalog_config(base, stored):
    if stored is None:
        return base
    return {**base, PROFILE_KEY: stored['profiles'],
            ASSIGNMENT_KEY: stored['assignments'], REASONING_KEY: stored['reasoning_efforts'],
            DEFAULT_KEY: stored['default_profile_id']}


def effective_config():
    stored, _ = read_catalog()
    return catalog_config(current_app.config, stored)


def revision_for(config, stored):
    if stored is not None:
        return stored['revision']
    from services.ai_runtime_config_service import _profiles, _mapping
    # Include secret-only changes and the owning runtime without exposing hashes
    # of individual credentials in the public catalog.
    data = [_profiles(config), _mapping(config, ASSIGNMENT_KEY),
            _mapping(config, REASONING_KEY), config.get(DEFAULT_KEY),
            config.get('GAME_RUNTIME_ID')]
    return hmac.new(str(config['SECRET_KEY']).encode(),
                    json.dumps(data, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def text(value, label, limit=200, required=False):
    if not isinstance(value, str):
        raise ValueError(f'{label}必须是文本')
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f'{label}不能为空或长度超过限制' if required else f'{label}长度超过限制')
    return value


def address(value, label, required=False):
    value = text(value, label, 2000, required).rstrip('/')
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {'https', 'http'} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f'{label}须为不含账号、查询参数和片段的 HTTP(S) 地址')
    return value


def secret(raw, old, key):
    clear = raw.get('clear_' + key, False)
    if type(clear) is not bool:
        raise ValueError('清空凭证选项必须为布尔值')
    value = text(raw.get(key, ''), '凭证', 4000)
    return '' if clear else (value or str(old.get(key) or ''))


def normalize(payload, config):
    from services.ai_runtime_config_service import _profiles, _CAPABILITIES, _EFFORTS, AI_FEATURES_BY_SCOPE
    raw_profiles = payload.get('profiles')
    if not isinstance(raw_profiles, list) or not 1 <= len(raw_profiles) <= 64:
        raise ValueError('请保留 1 至 64 个模型预设')
    previous = {p['id']: p for p in _profiles(config)}
    profiles, ids = [], set()
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            raise ValueError('模型预设格式错误')
        pid = text(raw.get('id', ''), '预设 ID', 64, True)
        if not ID.fullmatch(pid) or pid in ids:
            raise ValueError('预设 ID 必须唯一，以小写字母开头，只能含小写字母、数字、下划线或短横线')
        ids.add(pid)
        old = previous.get(pid, {})
        provider = text(raw.get('provider', ''), '协议', required=True)
        if provider not in _CAPABILITIES:
            raise ValueError('不支持的厂商协议')
        stream = raw.get('stream_enabled', old.get('stream_enabled', False))
        if type(stream) is not bool:
            raise ValueError('流式开关必须为布尔值')
        mode = raw.get('json_object_mode', old.get('json_object_mode', 'auto'))
        if mode not in {'auto', 'enabled', 'disabled'}:
            raise ValueError('不支持的 JSON 模式')
        item = {
            'id': pid, 'name': text(raw.get('name', ''), '预设名称', 80, True),
            'provider': provider, 'api_base': address(raw.get('api_base', ''), '接口地址', True),
            'model': text(raw.get('model', ''), '模型名称', 200, True),
            'stream_enabled': stream, 'json_object_mode': mode,
            'backup_profile_id': text(raw.get('backup_profile_id', old.get('backup_profile_id', '')), '备用预设 ID', 64),
            'backup_api_base': address(raw.get('backup_api_base', old.get('backup_api_base', '')), '备用接口地址'),
            'backup_model': text(raw.get('backup_model', old.get('backup_model', '')), '备用模型'),
            **{key: secret(raw, old, key) for key in ('api_key', 'backup_api_key', 'balance_session')},
        }
        pool = raw.get('backup_api_keys', old.get('backup_api_keys', []))
        if not isinstance(pool, list) or len(pool) > 32:
            raise ValueError('同线路备用 Key 最多 32 个')
        old_pool = {k.get('id'): k for k in old.get('backup_api_keys', []) if isinstance(k, dict)}
        keys, key_ids = [], set()
        for credential in pool:
            if not isinstance(credential, dict):
                raise ValueError('备用 Key 格式错误')
            kid = text(credential.get('id', ''), '备用 Key ID', 64, True)
            if not ID.fullmatch(kid) or kid in key_ids:
                raise ValueError('备用 Key ID 格式错误或重复')
            key_ids.add(kid)
            enabled = credential.get('enabled', True)
            if type(enabled) is not bool:
                raise ValueError('备用 Key 开关必须为布尔值')
            keys.append({'id': kid, 'name': text(credential.get('name', kid), '备用 Key 名称', 80),
                         'enabled': enabled, **{k: secret(credential, old_pool.get(kid, {}), k)
                         for k in ('api_key', 'balance_session')}})
        item['backup_api_keys'] = keys
        profiles.append(item)
    for item in profiles:
        bid = item['backup_profile_id']
        if bid and (bid not in ids or bid == item['id']):
            raise ValueError('备用预设必须指向另一个现有预设')
    default = payload.get('default_profile_id')
    if default not in ids:
        raise ValueError('请选择有效的默认预设')
    features = {f['key'] for f in AI_FEATURES_BY_SCOPE['s1']}
    assignments = payload.get('assignments', {})
    efforts = payload.get('reasoning_efforts', {})
    if not isinstance(assignments, dict) or not isinstance(efforts, dict):
        raise ValueError('用途分配和思考设置必须为对象')
    if any(k not in features or not isinstance(v, str) or v not in ids for k, v in assignments.items()):
        raise ValueError('用途分配引用了未知用途或不存在的预设')
    if any(k not in features or not isinstance(v, str) or v not in _EFFORTS for k, v in efforts.items()):
        raise ValueError('思考设置包含未知用途或无效选项')
    return {'profiles': profiles, 'default_profile_id': default,
            'assignments': assignments, 'reasoning_efforts': efforts}


def update_catalog(payload):
    from services.ai_runtime_config_service import STALE_AI_CONFIG_ERROR, MISSING_AI_CONFIG_REVISION_ERROR, get_ai_provider_config
    if not isinstance(payload, dict):
        return None, '模型配置格式错误'
    if not payload.get('revision'):
        return None, MISSING_AI_CONFIG_REVISION_ERROR
    stored, raw = read_catalog()
    config = catalog_config(current_app.config, stored)
    if payload['revision'] != revision_for(config, stored):
        return None, STALE_AI_CONFIG_ERROR
    try:
        catalog = normalize(payload, config)
    except (ValueError, TypeError) as exc:
        return None, str(exc)
    saved = save_catalog(catalog, raw)
    if saved is None:
        return None, STALE_AI_CONFIG_ERROR
    return get_ai_provider_config(scope='s1'), None
