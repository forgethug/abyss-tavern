import importlib.util
import json
from pathlib import Path
import sys

from dotenv import dotenv_values
import pytest

ROOT = Path(__file__).resolve().parents[2]


def tool(name):
    spec = importlib.util.spec_from_file_location('wizard_' + name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_key_roundtrip_preserves_special_characters_and_unrelated_settings(tmp_path, capsys):
    config = tool('configure')
    path = tmp_path / '.env'
    path.write_text('KEEP_ME=yes\n', encoding='utf-8')
    config.configure(path)
    before = dotenv_values(path)
    answers = iter(['https://model.example/v1/chat/completions', 'org/story-model'])
    key = "fixture-'quoted'\\value#${KEEP_ME}$tail"
    assert config.configure_model(path, ask=lambda _: next(answers), hidden=lambda _: key)
    values = dotenv_values(path)
    profile = json.loads(values['RUNTIME_MODEL_PROFILES_JSON'])[0]
    assert profile == {'id': 'default', 'provider': 'openai_compatible',
        'api_base': 'https://model.example/v1', 'api_key': key, 'model': 'org/story-model'}
    for field in ('KEEP_ME', 'SECRET_KEY', 'JWT_SECRET_KEY', 'RUNTIME_BOOTSTRAP_SECRET'):
        assert values[field] == before[field]
    assert key not in capsys.readouterr().out
    unchanged = path.read_bytes()
    assert not config.configure_model(path, ask=lambda _: '', hidden=lambda _: pytest.fail('Must not ask for key'))
    assert path.read_bytes() == unchanged


def test_edit_first_model_preserves_other_profiles_and_assignments(tmp_path):
    config = tool('configure')
    path = tmp_path / '.env'
    profiles = [{'id': 'first', 'provider': 'openai_compatible', 'api_base': 'https://a.example', 'model': 'a', 'api_key': 'old-fixture-key'},
                {'id': 'second', 'api_base': 'https://b.example', 'model': 'b', 'api_key': 'second-fixture-key'}]
    path.write_text('RUNTIME_MODEL_PROFILES_JSON=' + json.dumps(profiles) + '\nRUNTIME_MODEL_ASSIGNMENTS_JSON={"review":"second"}\n')
    answers = iter(['y', '', 'new-model'])
    config.configure_model(path, ask=lambda _: next(answers), hidden=lambda _: '')
    values = dotenv_values(path)
    actual = json.loads(values['RUNTIME_MODEL_PROFILES_JSON'])
    assert actual[0] == {**profiles[0], 'model': 'new-model'}
    assert actual[1] == profiles[1]
    assert json.loads(values['RUNTIME_MODEL_ASSIGNMENTS_JSON']) == {'review': 'second'}


@pytest.mark.parametrize('address', ['not-a-url', 'https://user:secret@example.test/v1',
    'https://example.test/v1?api_key=secret', 'https://example.test/#key', 'file:///private/key'])
def test_invalid_address_does_not_replace_config(tmp_path, address):
    config = tool('configure')
    path = tmp_path / '.env'
    path.write_text('KEEP_ME=yes\n')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        config.configure_model(path, ask=lambda _: address, hidden=lambda _: pytest.fail('Must validate URL first'))
    assert path.read_bytes() == before


def test_cancelled_key_input_does_not_write_partial_model(tmp_path):
    config = tool('configure')
    path = tmp_path / '.env'
    path.write_text('KEEP_ME=yes\n')
    answers = iter(['https://example.test/v1', 'fixture-model'])
    before = path.read_bytes()
    def cancel(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        config.configure_model(path, ask=lambda _: next(answers), hidden=cancel)
    assert path.read_bytes() == before


def test_setup_uses_private_config_path_and_reexecutes_inside_venv(tmp_path, monkeypatch):
    setup = tool('setup')
    calls = []
    monkeypatch.setattr(setup, 'ROOT', tmp_path)
    monkeypatch.setattr(setup, 'check_environment', lambda: None)
    monkeypatch.setattr(setup.subprocess, 'run', lambda args, **kw: calls.append(args))
    monkeypatch.setattr(setup.subprocess, 'call', lambda args, **kw: calls.append(args) or 0)
    monkeypatch.setenv('PLATFORM_ENV_FILE', str(tmp_path / 'unused.env'))
    monkeypatch.setenv('PYTHONUTF8', '1')
    path = tmp_path / 'private settings/config.env'
    assert setup.main(['--env-file', str(path), '--no-start']) == 0
    assert setup.os.environ['PLATFORM_ENV_FILE'] == str(path)
    assert calls[0] == [sys.executable, str(tmp_path / 'tools/install.py')]
    assert Path(calls[1][0]).parent.parent == tmp_path / '.venv'
    assert '--configure-only' in calls[1] and '--no-start' in calls[1]
    assert calls[1][-2] == str(path)


def test_setup_admin_creation_is_idempotent_and_password_is_hashed(app, tmp_path, monkeypatch):
    import app as app_module
    import click
    from config import Config
    from extensions import db
    from models import User
    from werkzeug.security import check_password_hash
    setup = tool('setup')
    monkeypatch.setattr(app_module, 'create_app', lambda: app)
    monkeypatch.setattr(Config, 'RUNTIME_DATA_DIR', str(tmp_path))
    values = iter(['wizard_admin', 'private-fixture-password'])
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: next(values))
    setup.ensure_admin()
    with app.app_context():
        user = db.session.query(User).filter_by(username='wizard_admin').one()
        assert user.is_admin and user.is_active
        original = user.password_hash
        assert check_password_hash(original, 'private-fixture-password')
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: pytest.fail('Must preserve administrator'))
    setup.ensure_admin()
    with app.app_context():
        assert db.session.query(User).filter_by(username='wizard_admin').one().password_hash == original


def test_setup_admin_never_promotes_or_overwrites_existing_player(app, make_user, tmp_path, monkeypatch):
    import app as app_module
    import click
    from config import Config
    from extensions import db
    from models import User
    setup = tool('setup')
    user_id, _ = make_user('existing_player')
    monkeypatch.setattr(app_module, 'create_app', lambda: app)
    monkeypatch.setattr(Config, 'RUNTIME_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: 'existing_player')
    with pytest.raises(setup.SetupError, match='不会覆盖'):
        setup.ensure_admin()
    with app.app_context():
        assert not db.session.get(User, user_id).is_admin


def test_start_checks_installation_and_passes_custom_env(tmp_path, monkeypatch):
    start = tool('start')
    monkeypatch.setattr(start, 'ROOT', tmp_path)
    env = tmp_path / 'private/settings.env'
    with pytest.raises(SystemExit, match='尚未完成安装'):
        start.main(['--env-file', str(env)])
    python = tmp_path / '.venv' / ('Scripts/python.exe' if start.os.name == 'nt' else 'bin/python')
    for path in (python, env, tmp_path / 'frontend-v2/dist/index.html'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    calls = []
    class Child:
        def wait(self):
            return 0
    monkeypatch.setattr(start.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)) or Child())
    assert start.main(['--env-file', str(env)]) == 0
    assert calls[0][1]['env']['PLATFORM_ENV_FILE'] == str(env)
    assert calls[0][0] == [str(python), '-X', 'utf8', str(tmp_path / 'backend/serve.py')]
