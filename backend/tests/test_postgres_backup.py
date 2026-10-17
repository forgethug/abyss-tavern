"""Opt-in real PostgreSQL backup/restore, using an isolated disposable cluster."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest
import psycopg
from psycopg import sql


@pytest.mark.skipif(not os.getenv('PG_BIN'), reason='Set PG_BIN to test against an isolated local PostgreSQL cluster')
def test_postgres_backup_restore_two_databases(tmp_path):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('backup_tool', root / 'tools/backup.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    binaries = Path(os.environ['PG_BIN'])
    executable_suffix = '.exe' if os.name == 'nt' else ''
    cluster = tmp_path / 'cluster'
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    def run(name, *args):
        subprocess.run([str(binaries / (name + executable_suffix)), *map(str, args)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, creationflags=flags)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    run('initdb', '-D', cluster, '-A', 'trust', '-U', 'postgres', '--no-locale', '--encoding=UTF8')
    run('pg_ctl', '-D', cluster, '-l', tmp_path / 'postgres.log', '-o', f'-h 127.0.0.1 -p {port}', '-w', 'start')
    try:
        def connect(database='postgres'):
            return psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname=database, autocommit=True)
        with connect() as connection:
            for prefix in ('source', 'restored'):
                for suffix in ('platform', 'a', 'b'):
                    connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(prefix + '_' + suffix)))
        with connect('source_platform') as connection:
            connection.execute('CREATE TABLE taverns (id INTEGER, slug TEXT, game_pack TEXT, runtime_namespace TEXT, runtime_pid INTEGER, runtime_status TEXT)')
            for number, name in enumerate(('a', 'b'), 1):
                connection.execute('INSERT INTO taverns VALUES (%s,%s,%s,%s,NULL,%s)', (number, name, 's1_fantasy', 'runtime-' + name, 'stopped'))
        for name in ('a', 'b'):
            with connect('source_' + name) as connection:
                connection.execute('CREATE TABLE evidence (value TEXT)')
                connection.execute('INSERT INTO evidence VALUES (%s)', (name,))
        def config(prefix):
            base = f'postgresql+psycopg://postgres@127.0.0.1:{port}/'
            return SimpleNamespace(SQLALCHEMY_DATABASE_URI=base + prefix + '_platform',
                RUNTIME_DATABASE_URL_TEMPLATE=base + prefix + '_{slug}', RUNTIME_DATA_DIR=str(tmp_path / prefix))
        tool.backup(tmp_path / 'backup', config('source'))
        tool.restore(tmp_path / 'backup', config('restored'))
        with connect('restored_platform') as connection:
            assert connection.execute('SELECT count(*) FROM taverns').fetchone() == (2,)
        for name in ('a', 'b'):
            with connect('restored_' + name) as connection:
                assert connection.execute('SELECT value FROM evidence').fetchall() == [(name,)]
        with pytest.raises(ValueError, match='empty PostgreSQL'):
            tool.restore(tmp_path / 'backup', config('restored'))
    finally:
        run('pg_ctl', '-D', cluster, '-m', 'fast', '-w', 'stop')
