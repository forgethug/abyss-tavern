import sys
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import create_app
from config import TestConfig
from db_repository import db_manager
from extensions import db
from models import User
from utils.auth import generate_token


@pytest.fixture
def app(request, tmp_path):
    config = TestConfig
    if getattr(request, 'param', None) == 'file':
        config = type('FileTestConfig', (TestConfig,), {
            'SQLALCHEMY_DATABASE_URI': f"sqlite:///{(tmp_path / 'platform.db').as_posix()}",
        })
    app = create_app(config)
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(app):
    def factory(username: str, is_admin: bool = False):
        with app.app_context():
            user = User(
                username=username,
                display_name=username.title(),
                password_hash=generate_password_hash("secret123"),
                is_admin=is_admin,
            )
            ok, error = db_manager.add(user)
            assert ok, error
            ok, error = db_manager.commit()
            assert ok, error
            token = generate_token(user.id)
            return user.id, {"Authorization": f"Bearer {token}"}

    return factory

