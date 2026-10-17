import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import jwt
import pytest

from app import create_app
from config import TestConfig
from runtime_core.process_lock import instance_lock
from runtime_core.secrets import validate_secret
from serve import register_frontend

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize('value', ['', ' ', 'short', 'replace-with-a-long-random-secret', 'same-as-control-plane-' + 'x' * 32])
@pytest.mark.parametrize('key', ['SECRET_KEY', 'JWT_SECRET_KEY', 'RUNTIME_BOOTSTRAP_SECRET'])
def test_public_or_missing_secrets_fail_before_creating_database(tmp_path, key, value):
    database = tmp_path / 'not-created.db'
    config = type('Invalid', (TestConfig,), {key: value, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{database.as_posix()}'})
    with pytest.raises(RuntimeError, match=key):
        create_app(config)
    assert not database.exists()


def test_configure_generates_persistent_distinct_secrets_and_preserves_settings(tmp_path):
    configure = module('configure').configure
    destination = tmp_path / '.env'
    destination.write_text('CUSTOM_VALUE=keep-me\nSECRET_KEY=replace-me\n', encoding='utf-8')
    configure(destination)
    original = destination.read_bytes()
    configure(destination)
    assert destination.read_bytes() == original
    from dotenv import dotenv_values
    values = dotenv_values(destination)
    secrets = [validate_secret(key, values[key]) for key in ('SECRET_KEY', 'JWT_SECRET_KEY', 'RUNTIME_BOOTSTRAP_SECRET')]
    assert len(set(secrets)) == 3
    assert values['CUSTOM_VALUE'] == 'keep-me'


def test_old_public_key_cannot_impersonate_real_user(client, make_user):
    user_id, good_headers = make_user('public-key-control')
    token = jwt.encode({'user_id': user_id, 'exp': 4102444800}, 'replace-with-another-long-random-secret', algorithm='HS256')
    assert client.get('/api/auth/me', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert client.get('/api/auth/me', headers=good_headers).status_code == 200


def test_configure_uses_same_dotenv_semantics_as_startup(tmp_path):
    from dotenv import dotenv_values
    path = tmp_path / '.env'
    valid = 'a-private-exported-flask-key-with-40-bytes'
    path.write_text(f'export SECRET_KEY = "{valid}" # keep this\nJWT_SECRET_KEY=short # this comment is not secret material\nRUNTIME_BOOTSTRAP_SECRET=${{SECRET_KEY}}-bootstrap\n', encoding='utf-8')
    module('configure').configure(path)
    result = dotenv_values(path)
    assert result['SECRET_KEY'] == valid
    assert result['RUNTIME_BOOTSTRAP_SECRET'] == valid + '-bootstrap'
    validate_secret('JWT_SECRET_KEY', result['JWT_SECRET_KEY'])
    assert 'keep this' in path.read_text(encoding='utf-8')


def test_static_spa_preserves_api_and_asset_404(app, tmp_path):
    (tmp_path / 'index.html').write_text('frontend-marker', encoding='utf-8')
    register_frontend(app, tmp_path)
    client = app.test_client()
    assert client.get('/taverns/demo').data == b'frontend-marker'
    for route in ('/api/unknown', '/assets/missing.js', '/missing.css', '/../private.env', '/assets/CON.txt', '/assets/sub/NUL.txt'):
        assert client.get(route).status_code == 404
    assert client.get('/api/health').json['code'] == 200


def test_dependency_update_preserves_bearer_cors_preflight(client):
    response = client.options('/api/auth/me', headers={
        'Origin': 'https://client.example',
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'Authorization, Content-Type',
    })
    assert response.status_code == 200
    assert response.headers['Access-Control-Allow-Origin'] == 'https://client.example'
    assert 'Authorization' in response.headers['Access-Control-Allow-Headers']
    assert 'Access-Control-Allow-Credentials' not in response.headers
    assert client.get('/api/auth/me', headers={'Origin': 'https://client.example'}).status_code == 401


def test_instance_lock_rejects_second_owner_and_releases(tmp_path):
    with instance_lock(tmp_path):
        with pytest.raises(RuntimeError, match='already running'):
            with instance_lock(tmp_path):
                pass
    with instance_lock(tmp_path):
        pass


def config(directory):
    return SimpleNamespace(SQLALCHEMY_DATABASE_URI=f"sqlite:///{(directory / 'platform.db').as_posix()}",
                           RUNTIME_DATA_DIR=str(directory / 'runtimes'), RUNTIME_DATABASE_URL_TEMPLATE='')


def seed(directory):
    directory.mkdir()
    (directory / 'runtimes').mkdir()
    with sqlite3.connect(directory / 'platform.db') as db:
        db.execute('CREATE TABLE taverns (id INTEGER, slug TEXT, game_pack TEXT, runtime_namespace TEXT, runtime_pid INTEGER, runtime_status TEXT)')
        for number, namespace in enumerate(('tavern-a', 'tavern-b'), 1):
            db.execute('INSERT INTO taverns VALUES (?, ?, ?, ?, NULL, ?)', (number, namespace, 's1_fantasy', namespace, 'stopped'))
            with sqlite3.connect(directory / 'runtimes' / (namespace + '.db')) as runtime:
                runtime.execute('CREATE TABLE evidence (value TEXT)')
                runtime.execute('INSERT INTO evidence VALUES (?)', (namespace,))


def test_backup_restore_two_taverns_and_refuse_overwrite(tmp_path):
    tool = module('backup')
    original, restored, archive = tmp_path / 'original', tmp_path / 'restored', tmp_path / 'archive'
    seed(original)
    tool.backup(archive, config(original))
    tool.restore(archive, config(restored))
    for name in ('tavern-a', 'tavern-b'):
        with sqlite3.connect(restored / 'runtimes' / (name + '.db')) as db:
            assert db.execute('SELECT value FROM evidence').fetchall() == [(name,)]
    with pytest.raises(ValueError, match='existing'):
        tool.restore(archive, config(restored))
    (archive / 'tavern-a.sqlite').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        tool.restore(archive, config(tmp_path / 'corrupt-target'))
    assert not (tmp_path / 'corrupt-target/platform.db').exists()


def test_backup_blocks_live_platform_and_live_runtime(tmp_path):
    tool = module('backup')
    seed(tmp_path / 'source')
    conf = config(tmp_path / 'source')
    with instance_lock(conf.RUNTIME_DATA_DIR), pytest.raises(RuntimeError):
        tool.backup(tmp_path / 'blocked', conf)
    with sqlite3.connect(tmp_path / 'source/platform.db') as db:
        db.execute('UPDATE taverns SET runtime_pid=123')
    with pytest.raises(RuntimeError, match='Stop all'):
        tool.backup(tmp_path / 'blocked', conf)
    assert not (tmp_path / 'blocked').exists()


def test_public_export_allowlist_and_secret_detection(tmp_path, monkeypatch):
    tool = module('export_public')
    for path in ('.env', '.git/config', '.codex/skills/private.md', 'backend/data/game.db',
                 'frontend-v2/node_modules/test/index.js', 'backend/error.log', 'docs/private-notes.md'):
        assert not tool.allowed(path)
    assert tool.allowed('backend/app.py')
    assert tool.allowed('.env.example')
    assert tool.allowed('.gitattributes')
    for path in ('README.en.md', 'docs/DEPLOYMENT.en.md', 'game-packs/s1-fantasy/PLAYING.en.md'):
        assert tool.allowed(path)
    for path in ('docs/08-s1-runtime-host.md', 'game-packs/s1-fantasy/source-snapshot/docs/adventurer_design.md',
                 'game-packs/s1-fantasy/source-snapshot/docs/s1_prompt_authoring_rules.md',
                 'game-packs/original-anomaly/source-snapshot/docs/s0-anomaly.md', 'runtime_host/internal-notes.md'):
        assert not tool.allowed(path)
    assert tool.allowed('LICENSE')
    assert any(pattern.search(b'sk-' + b'a' * 36) for pattern in tool.SECRET_PATTERNS)
    assert not tool.allowed('../backend/app.py')


def test_snapshot_bytes_survive_windows_git_checkout(tmp_path):
    source, checkout = tmp_path / 'source', tmp_path / 'checkout'
    source.mkdir()
    subprocess.run(['git', 'init', str(source)], check=True, capture_output=True)
    (source / '.gitattributes').write_bytes((ROOT / '.gitattributes').read_bytes())
    samples = {}
    for pack in ('s1-fantasy', 'original-anomaly'):
        for ending, data in (('lf', b'first\nsecond\n'), ('crlf', b'first\r\nsecond\r\n')):
            relative = f'game-packs/{pack}/source-snapshot/{ending}.py'
            samples[relative] = data
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    subprocess.run(['git', '-c', 'core.autocrlf=false', 'add', '.'], cwd=source, check=True, capture_output=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-m', 'Snapshot fixture'], cwd=source, check=True, capture_output=True)
    subprocess.run(['git', '-c', 'core.autocrlf=true', 'clone', '--no-local', str(source), str(checkout)],
                   check=True, capture_output=True)
    for relative, data in samples.items():
        assert (checkout / relative).read_bytes() == data
