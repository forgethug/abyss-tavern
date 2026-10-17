"""Upgrade transaction tests use disposable installs and no model requests."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from types import SimpleNamespace
import urllib.error
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import updater
import release_format as fmt
import update
from runtime_core.installation import installed_python, require_completed_update
from runtime_core.process_lock import instance_lock
from test_release_operations import module, seed, config


def manifest(files, version='0.2.0'):
    return dict(format=1, repository=fmt.REPOSITORY, version=version, data_format=1,
                commit='a' * 40, files={n: hashlib.sha256(v).hexdigest() for n, v in files.items()})


def package(path, files, metadata=None):
    metadata = metadata or manifest(files)
    with zipfile.ZipFile(path, 'w') as output:
        for name, content in files.items():
            output.writestr(name, content)
        output.writestr(fmt.MANIFEST, json.dumps(metadata))
    return path


def install(tmp_path):
    root = tmp_path / 'installation'
    seed(root)
    files = {'README.md': b'old', 'backend/obsolete.py': b'old code', 'LICENSE': b'unchanged'}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / '.env').write_bytes(b'MODEL_KEY=private-fixture-value\n')
    (root / 'frontend-v2/dist').mkdir(parents=True)
    (root / 'frontend-v2/dist/index.html').write_bytes(b'old frontend')
    (root / fmt.MANIFEST).write_text(json.dumps(manifest(files, '0.1.0')), encoding='utf8')
    return root


def prepared(candidate, backup):
    assert (backup / 'platform.sqlite').is_file()
    python = candidate / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    python.parent.mkdir(parents=True)
    python.write_bytes(b'fixture interpreter')
    (candidate / 'frontend-v2/dist').mkdir(parents=True)
    (candidate / 'frontend-v2/dist/index.html').write_bytes(b'new frontend')
    return python


def next_package(tmp_path):
    return package(tmp_path / 'next.zip', {'README.md': b'new', 'LICENSE': b'unchanged',
                                         'backend/new.py': b'new code'})


def private_state(root):
    return {name: (root / name).read_bytes() for name in (
        '.env', 'platform.db', 'runtimes/tavern-a.db', 'runtimes/tavern-b.db')}


@pytest.mark.parametrize('name', ['.env', 'backend/data/a.db', '../README.md', '/README.md',
    'backend/../README.md', 'backend/CON.py', 'backend/a:stream', 'backend/a\\b.py',
    'backend/a. /b.py', '.updates/current.json', 'docs/private-notes.md', 'backend/.ENV',
    'backend/.GIT/config', 'backend/.UPDATES/private.json'])
def test_package_rejects_private_and_unsafe_paths(tmp_path, name):
    with pytest.raises(ValueError):
        fmt.unpack_package(package(tmp_path / 'bad.zip', {name: b'x'}), tmp_path / 'out')


@pytest.mark.parametrize('kind', ['checksum', 'extra', 'missing', 'collision', 'format', 'symlink'])
def test_package_rejects_invalid_contents(tmp_path, kind):
    files = {'backend/a.py': b'x'}
    meta = manifest(files)
    if kind == 'checksum':
        files['backend/a.py'] = b'wrong'
    elif kind == 'extra':
        files['backend/b.py'] = b'extra'
    elif kind == 'missing':
        files = {}
    elif kind == 'collision':
        files['backend/A.py'] = b'x'
        meta = manifest(files)
    elif kind == 'format':
        meta['data_format'] = 2
    path = package(tmp_path / 'bad.zip', files, meta)
    if kind == 'symlink':
        with zipfile.ZipFile(path, 'a') as output:
            entry = zipfile.ZipInfo('backend/link.py')
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(entry, 'a.py')
    with pytest.raises(ValueError):
        fmt.unpack_package(path, tmp_path / 'out')


def test_success_preserves_private_data_and_only_replaces_changed_source(tmp_path):
    root = install(tmp_path)
    private = private_state(root)
    original_mtime = (root / 'LICENSE').stat().st_mtime_ns
    (root / 'my-notes.txt').write_bytes(b'keep')
    with instance_lock(root / '.updates'), instance_lock(config(root).RUNTIME_DATA_DIR):
        version, result = updater.upgrade(root, next_package(tmp_path), module('backup'), config(root),
                                          root / '.env', prepare=prepared)
    assert version == '0.2.0'
    assert result['changed'] == 2 and result['removed'] == 1
    assert private_state(root) == private
    assert (root / 'my-notes.txt').read_bytes() == b'keep'
    assert (root / 'LICENSE').stat().st_mtime_ns == original_mtime
    assert not (root / 'backend/obsolete.py').exists()
    assert (root / 'frontend-v2/dist/index.html').read_bytes() == b'new frontend'
    assert installed_python(root).is_file()
    assert updater.baseline(root)['version'] == '0.2.0'
    assert not (root / '.updates/pending.json').exists()
    backup = Path(result['backup'])
    assert (backup.parent / 'private.env').read_bytes() == private['.env']
    for name in ('tavern-a', 'tavern-b'):
        with sqlite3.connect(backup / (name + '.sqlite')) as connection:
            assert connection.execute('SELECT value FROM evidence').fetchall() == [(name,)]


def test_preparation_failure_does_not_change_installation(tmp_path):
    root = install(tmp_path)
    private = private_state(root)
    def fail(*args):
        raise RuntimeError('dependency or startup failure')
    with pytest.raises(RuntimeError, match='startup failure'):
        updater.upgrade(root, next_package(tmp_path), module('backup'), config(root), root / '.env', prepare=fail)
    assert private_state(root) == private
    assert (root / 'README.md').read_bytes() == b'old'
    assert (root / 'frontend-v2/dist/index.html').read_bytes() == b'old frontend'
    assert not (root / '.updates/pending.json').exists()


@pytest.mark.parametrize('failure_number', [1, 2, 3, 4])
def test_apply_failure_recovers_source_frontend_and_receipt(tmp_path, failure_number):
    root = install(tmp_path)
    private = private_state(root)
    transaction = root / '.updates/transactions/test'
    candidate = transaction / 'candidate'
    after = fmt.unpack_package(next_package(tmp_path), candidate)
    (transaction / 'databases').mkdir()
    (transaction / 'databases/platform.sqlite').touch()
    python = prepared(candidate, transaction / 'databases')
    calls = 0
    def fail(path, content):
        nonlocal calls
        calls += 1
        if calls == failure_number:
            raise OSError('simulated write failure')
        updater.atomic_bytes(path, content)
    with pytest.raises(OSError):
        updater.apply_candidate(root, transaction, updater.baseline(root), after, python, replace_file=fail)
    assert private_state(root) == private
    assert (root / 'README.md').read_bytes() == b'old'
    assert (root / 'backend/obsolete.py').read_bytes() == b'old code'
    assert not (root / 'backend/new.py').exists()
    assert (root / 'frontend-v2/dist/index.html').read_bytes() == b'old frontend'
    assert updater.baseline(root)['version'] == '0.1.0'
    assert not (root / '.updates/pending.json').exists()


def test_interrupted_recovery_blocks_startup_and_is_retryable(tmp_path, monkeypatch):
    root = install(tmp_path)
    recover = updater.recover
    def crash(*args):
        raise OSError('power interruption')
    monkeypatch.setattr(updater, 'recover', crash)
    transaction = root / '.updates/transactions/interrupted'
    candidate = transaction / 'candidate'
    after = fmt.unpack_package(next_package(tmp_path), candidate)
    (transaction / 'databases').mkdir()
    (transaction / 'databases/platform.sqlite').touch()
    python = prepared(candidate, transaction / 'databases')
    calls = 0
    def interrupt(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt()
        updater.atomic_bytes(path, content)
    with pytest.raises(OSError):
        updater.apply_candidate(root, transaction, updater.baseline(root), after, python, replace_file=interrupt)
    with pytest.raises(RuntimeError, match='升级未完成'):
        require_completed_update(root)
    monkeypatch.setattr(updater, 'recover', recover)
    assert recover(root)
    assert not recover(root)
    require_completed_update(root)
    assert (root / 'README.md').read_bytes() == b'old'


@pytest.mark.parametrize('kind', ['local-edit', 'collision', 'downgrade', 'format'])
def test_conflicts_fail_before_backup(tmp_path, kind):
    root = install(tmp_path)
    before = updater.baseline(root)
    after = manifest({'README.md': b'new', 'backend/new.py': b'new'})
    if kind == 'local-edit':
        (root / 'README.md').write_bytes(b'personal edit')
    elif kind == 'collision':
        (root / 'backend/new.py').write_bytes(b'my own code')
    elif kind == 'downgrade':
        after['version'] = '0.1.0'
    elif kind == 'format':
        after['data_format'] = 2
    with pytest.raises(ValueError):
        updater.plan_update(root, before, after)
    assert not (root / '.updates').exists()


def test_initial_baseline_verifies_official_git_blobs(tmp_path, monkeypatch):
    content = b'initial public source'
    (tmp_path / 'README.md').write_bytes(content)
    blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
    def get(url):
        assert updater.INITIAL_COMMIT in url
        return {'tree': [dict(path='README.md', type='blob', mode='100644', sha=blob)]}
    monkeypatch.setattr(updater, 'get_json', get)
    assert updater.baseline(tmp_path)['version'] == '0.1.0'
    (tmp_path / 'README.md').write_bytes(b'local changes')
    with pytest.raises(ValueError):
        updater.baseline(tmp_path)


def test_case_only_rename_cannot_delete_new_file_on_windows(tmp_path):
    root = install(tmp_path)
    before = updater.baseline(root)
    after = manifest({'README.md': b'new', 'backend/OBSOLETE.py': b'new code'})
    with pytest.raises(ValueError, match='大小写'):
        updater.plan_update(root, before, after)
    assert (root / 'backend/obsolete.py').read_bytes() == b'old code'


def test_no_release_is_a_normal_read_only_result(monkeypatch, tmp_path, capsys):
    root = install(tmp_path)
    def missing(url):
        raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
    monkeypatch.setattr(updater, 'get_json', missing)
    assert update.main(['--root', str(root), '--check']) == 0
    assert '暂无正式升级包' in capsys.readouterr().out
    assert not (root / '.updates').exists()


@pytest.mark.parametrize('bad', ['url', 'digest', 'prerelease', 'size'])
def test_release_metadata_rejects_untrusted_downloads(monkeypatch, bad):
    asset = dict(name='abyss-tavern-v0.2.0.zip', state='uploaded', size=123,
                 digest='sha256:' + 'a' * 64,
                 browser_download_url='https://github.com/w7lau/abyss-tavern/releases/download/v0.2.0/abyss-tavern-v0.2.0.zip')
    release = dict(tag_name='v0.2.0', assets=[asset])
    if bad == 'url':
        asset['browser_download_url'] = 'https://other.example/release.zip'
    elif bad == 'digest':
        asset['digest'] = None
    elif bad == 'prerelease':
        release['prerelease'] = True
    else:
        asset['size'] = fmt.MAX_PACKAGE_BYTES + 1
    monkeypatch.setattr(updater, 'get_json', lambda url: release)
    with pytest.raises(ValueError):
        updater.latest_release()


def test_candidate_environment_excludes_private_settings(monkeypatch):
    for name in ('DEEPSEEK_API_KEY', 'DATABASE_URL', 'PLATFORM_ENV_FILE', 'JWT_SECRET_KEY'):
        monkeypatch.setenv(name, 'private')
    env = updater.clean_environment()
    assert not any(name in env for name in ('DEEPSEEK_API_KEY', 'DATABASE_URL', 'PLATFORM_ENV_FILE', 'JWT_SECRET_KEY'))


def test_environment_receipt_cannot_escape_installation(tmp_path):
    (tmp_path / '.updates').mkdir()
    (tmp_path / '.updates/current.json').write_text(json.dumps({'python': '../outside/python'}))
    with pytest.raises(RuntimeError):
        installed_python(tmp_path)


def test_update_is_excluded_from_public_export():
    from export_public import allowed
    assert allowed('VERSION')
    assert not allowed('.updates/transactions/private.env')
    assert not allowed('.updates/transactions/candidate/backend/app.py')


def test_linked_venv_interpreter_retains_environment_path(tmp_path):
    python = tmp_path / '.updates/candidate/.venv/bin/python'
    python.parent.mkdir(parents=True)
    try:
        python.symlink_to(sys.executable)
    except OSError:
        pytest.skip('Creating symlinks is not permitted on this Windows host')
    (tmp_path / '.updates/current.json').write_text(json.dumps({'python': python.relative_to(tmp_path).as_posix()}))
    assert installed_python(tmp_path) == python
    assert update.active_python(tmp_path) == python


def test_release_builder_uses_committed_source_and_rejects_version_mismatch(tmp_path, monkeypatch):
    import subprocess
    import export_public
    import build_release
    source = tmp_path / 'public-source'
    source.mkdir()
    files = {'VERSION': b'0.2.0\n', 'LICENSE': b'fixture', 'THIRD_PARTY_NOTICES.md': b'fixture',
             '.env.example': b'EMPTY_KEY=\n', 'backend/serve.py': b'# fixture\n'}
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=source, stderr=subprocess.STDOUT)
    git('init')
    git('-c', 'core.autocrlf=false', 'add', '.')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Public fixture')
    (source / '.env').write_text('PRIVATE_KEY=fixture-only')
    (source / 'backend/serve.py').write_text('uncommitted change')
    monkeypatch.setattr(export_public, 'ROOT', source)
    archive = build_release.build(tmp_path / 'packages', 'HEAD', '0.2.0')
    unpacked = tmp_path / 'unpacked'
    metadata = fmt.unpack_package(archive, unpacked)
    assert metadata['commit'] == git('rev-parse', 'HEAD').decode().strip()
    assert (unpacked / 'backend/serve.py').read_bytes() == files['backend/serve.py']
    assert not (unpacked / '.env').exists()
    assert not (unpacked / '.git').exists()
    assert archive.with_suffix('.zip.sha256').read_text().split()[0] == fmt.digest(archive)
    with pytest.raises(ValueError, match='VERSION'):
        build_release.build(tmp_path / 'packages', 'HEAD', '0.3.0')


def test_download_checksum_and_size_are_verified(tmp_path, monkeypatch):
    import io
    body = b'fixture archive bytes'
    monkeypatch.setattr(updater.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(body))
    release = {'asset': dict(browser_download_url='https://github.com/fixture', size=len(body),
                            digest='sha256:' + hashlib.sha256(body).hexdigest())}
    updater.download_release(release, tmp_path / 'ok.zip')
    release['asset']['digest'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError, match='checksum'):
        updater.download_release(release, tmp_path / 'bad.zip')
    release['asset']['size'] = 1
    with pytest.raises(ValueError, match='size'):
        updater.download_release(release, tmp_path / 'oversize.zip')


def test_live_installation_lock_blocks_update_before_download(tmp_path, monkeypatch):
    root = install(tmp_path)
    import config as config_module
    monkeypatch.setattr(config_module, 'Config', config(root))
    (root / 'tools').mkdir()
    (root / 'tools/backup.py').write_text('def backup_locked(*args): raise AssertionError("must not run")')
    def unexpected():
        raise AssertionError('must not download')
    monkeypatch.setattr(update, 'latest_release', unexpected)
    with instance_lock(config(root).RUNTIME_DATA_DIR):
        with pytest.raises(RuntimeError, match='already running'):
            update.main(['--root', str(root), '--inside-env', '--yes', '--no-start'])


def test_real_candidate_probe_closes_sqlite_and_frontend_files(tmp_path):
    import subprocess
    import export_public
    candidate = tmp_path / 'candidate'
    export_public.export(candidate, working_tree=True)
    frontend = candidate / 'frontend-v2/dist'
    frontend.mkdir()
    (frontend / 'index.html').write_text('fixture frontend')
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'manifest.json').write_text(json.dumps({'databases': []}))
    env = updater.clean_environment()
    env['TEMP'] = env['TMP'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-X', 'utf8', str(candidate / 'tools/upgrade_probe.py'), str(backup)],
                            cwd=candidate, env=env, capture_output=True, text=True, encoding='utf8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.glob('upgrade-probe-*'))
    (frontend / 'index.html').unlink()  # No open send_file response on Windows.


def test_recovery_keeps_startup_guard_until_program_and_frontend_restored(tmp_path, monkeypatch):
    root = install(tmp_path)
    before = updater.baseline(root)
    files = {'README.md': b'new', 'LICENSE': b'unchanged', 'backend/serve.py': b'new guarded server',
             'tools/start.py': b'new guarded launcher', 'runtime_core/installation.py': b'guard'}
    transaction = root / '.updates/transactions/guarded'
    candidate = transaction / 'candidate'
    after = fmt.unpack_package(package(tmp_path / 'guarded.zip', files), candidate)
    (transaction / 'databases').mkdir()
    (transaction / 'databases/platform.sqlite').touch()
    python = prepared(candidate, transaction / 'databases')
    atomic = updater.atomic_bytes
    def fail_receipt(path, content):
        if path.name == 'current.json':
            raise OSError('interrupted receipt write')
        atomic(path, content)
    # These guard files were absent in the initial release, so recovering them
    # removes them. Inspect that moment without changing filesystem semantics.
    unlink = Path.unlink
    checked = []
    def check_before_unlink(path, *args, **kwargs):
        if path == root / 'backend/serve.py':
            assert (root / 'README.md').read_bytes() == b'old'
            assert (root / 'frontend-v2/dist/index.html').read_bytes() == b'old frontend'
            checked.append(True)
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', check_before_unlink)
    with pytest.raises(OSError):
        updater.apply_candidate(root, transaction, before, after, python, replace_file=fail_receipt)
    assert checked == [True]
    assert not (root / '.updates/pending.json').exists()
