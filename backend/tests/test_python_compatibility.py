from datetime import datetime, timezone
from pathlib import Path
import runpy

from runtime_core.time import utc_now, utc_from_timestamp


def test_clock_preserves_naive_utc_and_round_trips(app):
    from extensions import db
    from models.user import User

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    value = utc_now()
    assert value.tzinfo is None
    assert before <= value <= datetime.now(timezone.utc).replace(tzinfo=None)
    assert utc_from_timestamp(0) == datetime(1970, 1, 1)
    with app.app_context():
        user = User(username='clock-check', password_hash='unused')
        db.session.add(user)
        db.session.commit()
        identity = user.id
        assert user.created_at >= before
        db.session.expire_all()
        loaded = User.query.session.get(User, identity)
        assert loaded is user
        assert loaded.created_at.tzinfo is None
        assert User.query.session.get(User, -1) is None


def test_source_projection_preserves_text_defaults_and_filtered_queries():
    transform = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/portable_python_compat.py'))['modernize_python']
    source = '''"""日期 datetime.utcnow() must remain literal."""
from __future__ import annotations
from datetime import datetime as dt
label = "Model.query.get(1)"
factory = dt.utcnow
def now():
    return dt.utcnow()
def lookup(Model):
    return Model.query.get(1)
def filtered(Model):
    return Model.query.filter_by(active=True).get(1)
'''
    result = transform(source)
    namespace = {}
    exec(result, namespace)
    assert namespace['factory'] is utc_now
    assert namespace['now']().tzinfo is None
    assert namespace['label'] == 'Model.query.get(1)'
    assert 'Model.query.session.get(Model, 1)' in result
    assert 'Model.query.filter_by(active=True).get(1)' in result
    assert transform(result) == result


def test_projected_get_keeps_refresh_loader_and_lock_options(app):
    from extensions import db
    from models.user import User
    from sqlalchemy import inspect, update
    from sqlalchemy.orm import defer

    transform = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/portable_python_compat.py'))['modernize_python']
    namespace = {'User': User, 'defer': defer}
    exec(transform('''def lookup(identity):
    return User.query.populate_existing().options(defer(User.password_hash)).with_for_update(nowait=True).get(
        identity,  # preserve a trailing comma and comment
    )
'''), namespace)
    with app.app_context():
        user = User(username='before-refresh', password_hash='unused')
        db.session.add(user)
        db.session.commit()
        identity = user.id
        db.session.execute(update(User).where(User.id == identity).values(username='after-refresh').execution_options(synchronize_session=False))
        assert user.username == 'before-refresh'
        assert namespace['lookup'](identity).username == 'after-refresh'
        db.session.expunge_all()
        assert 'password_hash' in inspect(namespace['lookup'](identity)).unloaded
    # SQLite cannot prove PostgreSQL row locks; capture the exact Session.get
    # arguments independently to ensure the lock request was not dropped.
    from types import SimpleNamespace
    calls = []
    namespace['User'] = SimpleNamespace(password_hash='hash', query=SimpleNamespace(session=SimpleNamespace(get=lambda *a, **kw: calls.append((a, kw)))))
    namespace['defer'] = lambda value: ('defer', value)
    namespace['lookup'](7)
    assert calls[0][1] == {'populate_existing': True, 'with_for_update': {'nowait': True}, 'options': (('defer', 'hash'),)}
