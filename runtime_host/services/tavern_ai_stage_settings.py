"""Optional S1 stages, persisted independently in each tavern database."""
import json
from threading import RLock
from uuid import uuid4

from flask import current_app
from sqlalchemy import select, insert, update
from sqlalchemy.exc import IntegrityError

from extensions import db
from models.system import SystemConfig

KEY = 's1_ai_stage_settings'
FIELDS = ('appeal_second_review', 'expedition_observer', 'high_risk_adjudication')


def initialize():
    table = SystemConfig.__table__
    with db.engine.connect() as connection:
        raw = connection.execute(select(table.c.config_value).where(table.c.config_key == KEY)).scalar_one_or_none()
    value = json.loads(raw) if raw else {**dict.fromkeys(FIELDS, True), 'revision': 'default'}
    if set(value) != {*FIELDS, 'revision'} or any(type(value.get(k)) is not bool for k in FIELDS):
        raise ValueError('Invalid stored AI stage settings')
    current_app.extensions[KEY] = {'lock': RLock(), 'raw': raw, 'value': value}


def read_settings():
    state = current_app.extensions[KEY]
    with state['lock']:
        return dict(state['value'])


def enabled(stage):
    if stage not in FIELDS:
        raise ValueError('Unknown optional AI stage')
    return read_settings()[stage]


def save_settings(payload):
    if (not isinstance(payload, dict) or set(payload) != {*FIELDS, 'revision'}
            or any(type(payload.get(k)) is not bool for k in FIELDS)):
        return None, '请提供三个 AI 开关和当前配置版本', 400
    state = current_app.extensions[KEY]
    with state['lock']:
        if payload['revision'] != state['value']['revision']:
            return None, '配置已改变，请刷新后重试', 409
        value = {**{k: payload[k] for k in FIELDS}, 'revision': uuid4().hex}
        raw = json.dumps(value, sort_keys=True)
        table = SystemConfig.__table__
        try:
            with db.engine.begin() as connection:
                if state['raw'] is None:
                    connection.execute(insert(table).values(config_key=KEY, config_value=raw,
                        description='Current tavern optional AI stages'))
                else:
                    result = connection.execute(update(table).where(table.c.config_key == KEY,
                        table.c.config_value == state['raw']).values(config_value=raw))
                    if result.rowcount != 1:
                        return None, '配置已改变，请重启当前酒馆后重试', 409
        except IntegrityError:
            return None, '配置已改变，请重启当前酒馆后重试', 409
        state['raw'], state['value'] = raw, value
        return dict(value), None, 200
