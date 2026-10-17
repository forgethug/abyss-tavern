"""Atomic model catalog storage in the current runtime's own database.

One row is the transaction boundary. Compare-and-swap protects concurrent
editors, including the initial insert; no process-global credential cache.
"""
import json
from uuid import uuid4

from sqlalchemy import select, insert, update
from sqlalchemy.exc import IntegrityError

from extensions import db
from models.system import SystemConfig

KEY = 's1_ai_model_catalog'


def read_catalog():
    table = SystemConfig.__table__
    with db.engine.connect() as connection:
        raw = connection.execute(select(table.c.config_value).where(
            table.c.config_key == KEY
        )).scalar_one_or_none()
    return (json.loads(raw) if raw is not None else None), raw


def save_catalog(catalog, previous_raw):
    table = SystemConfig.__table__
    stored = {**catalog, 'revision': uuid4().hex}
    raw = json.dumps(stored, ensure_ascii=False, sort_keys=True)
    try:
        with db.engine.begin() as connection:
            if previous_raw is None:
                connection.execute(insert(table).values(
                    config_key=KEY, config_value=raw,
                    description='Current tavern AI model catalog',
                ))
            else:
                result = connection.execute(update(table).where(
                    table.c.config_key == KEY,
                    table.c.config_value == previous_raw,
                ).values(config_value=raw))
                if result.rowcount != 1:
                    return None
    except IntegrityError:
        # Another editor won the first insert. Never overwrite its credentials.
        return None
    return stored
