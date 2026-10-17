"""Per-runtime AI input policy; reads in prompt assembly never query the database."""
import json
from threading import RLock
from uuid import uuid4

from flask import current_app
from sqlalchemy import select, insert, update
from sqlalchemy.exc import IntegrityError

from extensions import db
from models.system import SystemConfig

KEY = 's1_ai_input_settings'
FLAG = 'ADVENTURER_S1_DESCRIPTION_AI_SUMMARY_ENABLED'


def _publish(value):
    current_app.extensions[KEY]['value'] = value
    current_app.config[FLAG] = value['compression_enabled']
    current_app.config['ADVENTURER_S1_TRAIT_CONTEXT_SELECTION_MODE'] = (
        'active' if value['compression_enabled'] else 'off'
    )


def initialize():
    current_app.extensions[KEY] = {'lock': RLock()}
    table = SystemConfig.__table__
    with db.engine.connect() as connection:
        raw = connection.execute(select(table.c.config_value).where(table.c.config_key == KEY)).scalar_one_or_none()
    value = json.loads(raw) if raw else {'compression_enabled': False, 'revision': 'default'}
    if type(value.get('compression_enabled')) is not bool:
        raise ValueError('Invalid stored AI input settings')
    current_app.extensions[KEY]['raw'] = raw
    _publish(value)


def enabled():
    return current_app.config.get(FLAG, False) is True


def read_settings():
    state = current_app.extensions[KEY]
    with state['lock']:
        return dict(state['value'])


def save_settings(payload):
    if (not isinstance(payload, dict) or set(payload) != {'compression_enabled', 'revision'}
            or type(payload.get('compression_enabled')) is not bool):
        return None, '请提供压缩开关和当前配置版本', 400
    state = current_app.extensions[KEY]
    with state['lock']:
        if payload['revision'] != state['value']['revision']:
            return None, '配置已改变，请刷新后重试', 409
        value = {'compression_enabled': payload['compression_enabled'], 'revision': uuid4().hex}
        raw = json.dumps(value, sort_keys=True)
        table = SystemConfig.__table__
        try:
            with db.engine.begin() as connection:
                if state['raw'] is None:
                    connection.execute(insert(table).values(config_key=KEY, config_value=raw,
                        description='Current tavern AI input compression'))
                else:
                    result = connection.execute(update(table).where(table.c.config_key == KEY,
                        table.c.config_value == state['raw']).values(config_value=raw))
                    if result.rowcount != 1:
                        return None, '配置已改变，请重启当前酒馆后重试', 409
        except IntegrityError:
            return None, '配置已改变，请重启当前酒馆后重试', 409
        state['raw'] = raw
        _publish(value)
        return dict(value), None, 200
