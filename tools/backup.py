"""Offline backup/restore. Run with the same PLATFORM_ENV_FILE as backend/serve.py."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT)]
if __name__ == '__main__':
    from runtime_core.installation import installed_python, require_completed_update
    require_completed_update(ROOT)
    active = installed_python(ROOT)
    if active.is_file() and os.path.abspath(sys.executable) != os.path.abspath(active):
        os.execv(str(active), [str(active), *sys.argv])
from config import Config
from runtime_core.process_lock import instance_lock
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def database_urls(config, taverns):
    result = [('platform', config.SQLALCHEMY_DATABASE_URI)]
    for row in taverns:
        namespace = row['runtime_namespace']
        if not re.fullmatch(r'[a-zA-Z0-9-]+', namespace):
            raise ValueError('Invalid runtime namespace')
        template = config.RUNTIME_DATABASE_URL_TEMPLATE
        url = template.format(runtime_id=namespace, runtime_key=namespace.replace('-', ''),
                              tavern_id=row['id'], slug=row['slug'], pack_id=row['game_pack']) if template else (
            f"sqlite:///{(Path(config.RUNTIME_DATA_DIR).resolve() / (namespace + '.db')).as_posix()}")
        result.append((namespace, url))
    if len({str(make_url(url)) for _, url in result}) != len(result):
        raise ValueError('Databases must be distinct')
    return result


def pg_command(tool, url, *arguments):
    parsed = make_url(url)
    executable = shutil.which(tool) or str(Path(os.getenv('PG_BIN', '')) / tool)
    env = os.environ.copy()
    env['PGPASSWORD'] = parsed.password or ''
    for key, value in parsed.query.items():
        if key not in {'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'connect_timeout', 'application_name', 'options'}:
            raise ValueError('Unsupported PostgreSQL connection option for backup')
        env['PG' + key.upper().replace('_', '')] = str(value)
    command = [executable, '--no-password', '--host', parsed.host or 'localhost', '--port', str(parsed.port or 5432),
               '--username', parsed.username or '', '--dbname', parsed.database, *arguments]
    result = subprocess.run(command, env=env, capture_output=True)
    if result.returncode:
        # Driver diagnostics may expose credentials; fail without printing them.
        raise RuntimeError(f'{tool} failed (exit {result.returncode}); check connectivity, database permissions and PG_BIN')


def read_taverns(url):
    parsed = make_url(url)
    if parsed.get_backend_name() == 'sqlite' and not Path(parsed.database).is_file():
        raise ValueError('Control database does not exist')
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(text(
                'SELECT id, slug, game_pack, runtime_namespace, runtime_pid, runtime_status FROM taverns'
            )).mappings()]
        if any(row['runtime_pid'] or row['runtime_status'] in ('ready', 'starting', 'running') for row in rows):
            raise RuntimeError('Stop all tavern runtimes cleanly before maintenance; stale runtime records require inspection')
        return rows
    finally:
        engine.dispose()


def backup(destination, config=Config):
    with instance_lock(config.RUNTIME_DATA_DIR):
        return backup_locked(destination, config)


def backup_locked(destination, config=Config):
    """Caller must hold instance_lock for the entire maintenance operation."""
    destination = Path(destination).resolve()
    taverns = read_taverns(config.SQLALCHEMY_DATABASE_URI)
    urls = database_urls(config, taverns)
    destination.mkdir(parents=True, exist_ok=False)
    entries = []
    for name, url in urls:
        parsed = make_url(url)
        backend = parsed.get_backend_name()
        if backend == 'sqlite':
            source = Path(parsed.database).resolve()
            if not source.is_file():
                if name == 'platform':
                    raise ValueError('Control database missing')
                continue  # Tavern has never started: no database to back up.
            filename = name + '.sqlite'
            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(destination / filename) as dst:
                src.backup(dst)
        elif backend == 'postgresql':
            filename = name + '.dump'
            pg_command('pg_dump', url, '--format=custom', '--no-owner', '--no-acl', '--file', str(destination / filename))
        else:
            raise ValueError('Only SQLite and PostgreSQL backup are supported')
        entries.append({'name': name, 'backend': backend, 'file': filename, 'sha256': digest(destination / filename)})
    sources = {str(path.relative_to(ROOT)): path.read_text(encoding='utf-8')
               for path in (ROOT / 'game-packs').glob('*/source-snapshot/SOURCE')}
    manifest = {'format': 1, 'taverns': taverns, 'databases': entries, 'sources': sources}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    return destination


def restore(source, config=Config):
    source = Path(source).resolve()
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['format'] != 1:
        raise ValueError('Unknown backup format')
    targets = dict(database_urls(config, manifest['taverns']))
    entries = manifest['databases']
    names = [entry['name'] for entry in entries]
    if len(set(names)) != len(names) or names.count('platform') != 1:
        raise ValueError('Duplicate or missing control database')
    with instance_lock(config.RUNTIME_DATA_DIR):
        # Validate every checksum and destination before creating any database.
        for entry in entries:
            filename = source / entry['file']
            if filename.parent != source or filename.is_symlink() or digest(filename) != entry['sha256']:
                raise ValueError('Backup path or checksum invalid')
            parsed = make_url(targets[entry['name']])
            if parsed.get_backend_name() != entry['backend']:
                raise ValueError('Restore must use the same database backend')
            if entry['backend'] == 'sqlite':
                if Path(parsed.database).exists():
                    raise ValueError('Restore refuses an existing SQLite database')
            else:
                engine = create_engine(targets[entry['name']])
                try:
                    if inspect(engine).get_table_names():
                        raise ValueError('Restore requires pre-created empty PostgreSQL databases')
                finally:
                    engine.dispose()
        for entry in entries:
            url = targets[entry['name']]
            if entry['backend'] == 'sqlite':
                destination = Path(make_url(url).database)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as output, (source / entry['file']).open('rb') as input_file:
                    shutil.copyfileobj(input_file, output)
            else:
                pg_command('pg_restore', url, '--exit-on-error', '--single-transaction', '--no-owner', '--no-acl', str(source / entry['file']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        (backup if args.operation == 'backup' else restore)(args.directory)
    except Exception as error:
        # SQLAlchemy errors can contain a connection URL, so no exception dump.
        parser.exit(1, f'Maintenance failed ({type(error).__name__}); no success receipt written. Check configuration and destinations.\n')
    print(f'{args.operation} completed: {args.directory.resolve()}')
