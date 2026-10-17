"""Read authoritative account IP evidence without persisting it in region 2."""

from types import SimpleNamespace

from flask import current_app

from db_repository import db_manager
from db_repository.s1_reservation_repository import S1ReservationRepository


_PROFILE_FIELDS = frozenset({
    'id', 'username', 'nickname', 'is_frozen', 'register_ip', 'last_login_ip',
})


def _batch_size():
    return int(current_app.config['ADVENTURER_S1_RESERVATION_ALT_USER_BATCH_SIZE'])


def validate_ip_user_ids(value):
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= _batch_size()
        or any(type(uid) is not int or uid < 1 for uid in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError('预约 IP 查询账号列表无效')
    return value


def read_central_ip_user_profiles(payload):
    """Called only after the existing runtime/deployment HMAC verification."""
    if current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE'):
        raise ValueError('预约 IP 证据只能由中央账号服务提供')
    if not isinstance(payload, dict) or set(payload) != {'user_ids'}:
        raise ValueError('预约 IP 查询参数无效')
    user_ids = validate_ip_user_ids(payload['user_ids'])
    rows = S1ReservationRepository.list_ip_user_profiles(user_ids)
    found = {row['id'] for row in rows}
    return {
        'users': rows,
        'missing_user_ids': [uid for uid in user_ids if uid not in found],
    }


def _parse_profiles(result, user_ids):
    if not isinstance(result, dict) or set(result) != {'users', 'missing_user_ids'}:
        raise ValueError('预约 IP 查询回执无效')
    rows, missing = result['users'], result['missing_user_ids']
    if not isinstance(rows, list) or not isinstance(missing, list):
        raise ValueError('预约 IP 查询回执无效')
    requested = set(user_ids)
    seen = set()
    users = {}
    for row in rows:
        if (
            not isinstance(row, dict) or set(row) != _PROFILE_FIELDS
            or type(row['id']) is not int or row['id'] not in requested
            or row['id'] in seen or type(row['is_frozen']) is not bool
        ):
            raise ValueError('预约 IP 查询账号回执无效')
        for key, limit in (
            ('username', 32), ('nickname', 64),
            ('register_ip', 45), ('last_login_ip', 45),
        ):
            value = row[key]
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise ValueError('预约 IP 查询账号回执无效')
        seen.add(row['id'])
        users[row['id']] = SimpleNamespace(**row)
    for uid in missing:
        if type(uid) is not int or uid not in requested or uid in seen:
            raise ValueError('预约 IP 查询缺失账号回执无效')
        seen.add(uid)
    if seen != requested:
        raise ValueError('预约 IP 查询回执不完整')
    return users


def load_ip_users(user_ids):
    user_ids = sorted(set(user_ids))
    if not user_ids:
        return {}
    if current_app.config.get('ADVENTURER_S1_FIXED_REALM_CODE') != 'region_2':
        return {
            row['id']: SimpleNamespace(**row)
            for row in S1ReservationRepository.list_ip_user_profiles(user_ids)
        }
    client = current_app.extensions.get('s1_active_slot_client')
    if client is None or not callable(getattr(client, 'get_ip_user_profiles', None)):
        raise RuntimeError('二区预约 IP 证据服务未就绪')
    if db_manager.transaction_depth():
        raise RuntimeError('预约 IP 证据不能在写入事务中读取')
    # Callers supply only scalar reservation/adventurer snapshots. No checked-out SQL
    # connection or ORM object may cross the bounded central HTTP call.
    db_manager.remove()
    users = {}
    size = _batch_size()
    for start in range(0, len(user_ids), size):
        batch = user_ids[start:start + size]
        try:
            users.update(_parse_profiles(client.get_ip_user_profiles(user_ids=batch), batch))
        except Exception as exc:
            raise RuntimeError('二区预约 IP 证据读取失败，请稍后重试') from exc
    return users
