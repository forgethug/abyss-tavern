"""Check candidate startup on disposable SQLite copies, with all HTTP disabled."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def block_network(*args, **kwargs):
    raise RuntimeError('Upgrade verification forbids network and AI calls')


def runtime_probe(database):
    snapshot = ROOT / 'game-packs/s1-fantasy/source-snapshot/backend'
    pack = json.loads((ROOT / 'game-packs/s1-fantasy/manifest.json').read_text(encoding='utf8'))
    os.environ.update(GAME_RUNTIME_IMPORT_PATHS_JSON=json.dumps([str(snapshot)]),
                      GAME_RUNTIME_PACK_ID='s1_fantasy', GAME_RUNTIME_PACK_RELEASE=pack['pack_version'],
                      GAME_RUNTIME_API_VERSION=pack['runtime_api_version'],
                      GAME_RUNTIME_SECRET_KEY='upgrade-probe-runtime-secret-32-bytes',
                      JWT_SECRET_KEY='upgrade-probe-jwt-secret-32-bytes',
                      GAME_RUNTIME_DATA_DIR=str(database.parent))
    sys.path[:0] = [str(ROOT / 'runtime_host'), str(ROOT)]
    from app import create_runtime_app
    app = create_runtime_app(database_uri=f'sqlite:///{database.as_posix()}', runtime_id='upgrade-probe',
                             bootstrap_secret='upgrade-probe-bootstrap-secret-32-bytes', seed_on_start=False,
                             adapter_path=str(ROOT / 'game-packs/s1-fantasy/runtime_adapter/s1_adapter.py'))
    assert app.test_client().get('/api/runtime/health').status_code == 200


def main(backup):
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf8'))
    with tempfile.TemporaryDirectory(prefix='upgrade-probe-') as temporary:
        directory = Path(temporary)
        platform = directory / 'platform.db'
        for entry in manifest['databases']:
            if entry['backend'] == 'sqlite':
                shutil.copyfile(backup / entry['file'], directory / (entry['name'] + '.db'))
        os.environ.update(DATABASE_URL=f'sqlite:///{platform.as_posix()}', RUNTIME_DATA_DIR=str(directory / 'runtimes'),
                          SECRET_KEY='upgrade-probe-platform-secret-32-bytes', JWT_SECRET_KEY='upgrade-probe-jwt-secret-32-bytes',
                          RUNTIME_BOOTSTRAP_SECRET='upgrade-probe-bootstrap-secret-32-bytes',
                          RUNTIME_AUTO_START='false', RUNTIME_SCHEDULER_ENABLED='false', RUNTIME_SEED_ON_START='false')
        sys.path[:0] = [str(ROOT / 'backend'), str(ROOT)]
        from app import create_app
        from serve import register_frontend
        from extensions import db
        app = create_app()
        register_frontend(app)
        client = app.test_client()
        try:
            with client.get('/api/health') as response:
                assert response.status_code == 200
            with client.get('/') as response:
                assert response.status_code == 200
        finally:
            # Windows cannot remove the disposable database while pooled
            # SQLite connections still hold the file open.
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        databases = [directory / (entry['name'] + '.db') for entry in manifest['databases']
                     if entry['name'] != 'platform' and entry['backend'] == 'sqlite']
        # PostgreSQL backups are verified by pg_dump, never restored over the live
        # database. The package still has to boot a fresh isolated runtime.
        for database in databases or [directory / 'empty-runtime.db']:
            subprocess.run([sys.executable, '-X', 'utf8', __file__, '--runtime', str(database)],
                           check=True, cwd=ROOT, env=os.environ.copy())
    print('Candidate platform and tavern startup verified; live databases unchanged.')


if __name__ == '__main__':
    try:
        socket.create_connection = block_network
        socket.socket.connect = block_network
        if sys.argv[1] == '--runtime':
            runtime_probe(Path(sys.argv[2]).resolve())
        else:
            main(Path(sys.argv[1]).resolve())
    except Exception as error:
        raise SystemExit(f'Candidate startup failed ({type(error).__name__}); live data unchanged.') from None
