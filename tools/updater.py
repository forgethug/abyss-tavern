"""Offline application of verified releases, with a recoverable file journal.

Production databases are backed up, but never migrated by this updater. Candidate
startup runs against copies. Data-format changes require a future migration
protocol; format-1 packages must remain compatible with existing databases.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

from release_format import (DATA_FORMAT, MANIFEST, MAX_PACKAGE_BYTES, REPOSITORY,
                            digest, safe_path, unpack_package, validate_manifest, version)

API = f'https://api.github.com/repos/{REPOSITORY}'
INITIAL_COMMIT = '8000704a7385e11bd1c869fc924c7997d3eb80d5'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.upgrade-tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf8'))


def internal_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Maintenance path crosses a link')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Maintenance path escapes installation')
    return path


def get_json(url):
    if not url.startswith(API + '/'):
        raise ValueError('Unexpected release API')
    request = urllib.request.Request(url, headers={'User-Agent': 'Abyss-Tavern-Updater', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Release metadata too large')
    return json.loads(data)


def latest_release():
    try:
        release = get_json(API + '/releases/latest')
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    tag = release.get('tag_name', '')
    if release.get('draft') or release.get('prerelease') or not tag.startswith('v'):
        raise ValueError('Not a stable published release')
    version(tag[1:])
    name = f'abyss-tavern-{tag}.zip'
    assets = [a for a in release.get('assets', []) if a['name'] == name and a.get('state') == 'uploaded']
    if len(assets) != 1:
        raise ValueError('Release has no unique upgrade package')
    asset = assets[0]
    if asset.get('browser_download_url') != f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}':
        raise ValueError('Unexpected release download URL')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or ''):
        raise ValueError('GitHub has not provided a SHA-256 digest for this package')
    if not 0 < asset.get('size', 0) <= MAX_PACKAGE_BYTES:
        raise ValueError('Invalid package size')
    return {'version': tag[1:], 'asset': asset}


def download_release(release, destination):
    asset = release['asset']
    request = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'Abyss-Tavern-Updater'})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, Path(destination).open('xb') as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > asset['size']:
                raise ValueError('Package exceeds declared size')
            output.write(chunk)
    if total != asset['size'] or digest(destination) != asset['digest'].removeprefix('sha256:'):
        raise ValueError('Package download checksum mismatch')


def baseline(root):
    receipt = root / '.updates/current.json'
    if receipt.exists():
        return validate_manifest(read_json(receipt)['manifest'])
    if (root / MANIFEST).exists():
        return validate_manifest(read_json(root / MANIFEST))
    # Bootstrap the original ZIP/Git installation without trusting arbitrary
    # local Git history. Git blob IDs are checked against the known public root.
    commit, installed_version = INITIAL_COMMIT, '0.1.0'
    if (root / 'VERSION').exists():
        installed_version = (root / 'VERSION').read_text(encoding='utf8').strip()
        version(installed_version)
        reference = get_json(API + '/git/ref/tags/v' + installed_version)['object']
        if reference['type'] == 'tag':
            reference = get_json(API + '/git/tags/' + reference['sha'])['object']
        if reference['type'] != 'commit' or not re.fullmatch(r'[0-9a-f]{40}', reference['sha']):
            raise ValueError('Version does not identify a published source commit')
        commit = reference['sha']
    tree = get_json(API + f'/git/trees/{commit}?recursive=1')
    if tree.get('truncated'):
        raise ValueError('Incomplete initial release tree')
    files = {}
    for entry in tree['tree']:
        if entry['type'] == 'tree':
            continue
        if entry['type'] != 'blob' or entry['mode'] not in {'100644', '100755'}:
            raise ValueError('Unsupported initial release object')
        path = safe_path(root, entry['path'])
        content = path.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if blob != entry['sha'] and '/source-snapshot/' not in entry['path'] and b'\0' not in content:
            # Git for Windows may check ordinary text out with CRLF. Snapshot
            # bytes remain exact, and all other source differences still fail.
            try:
                content.decode('utf8')
                canonical = content.replace(b'\r\n', b'\n')
                blob = hashlib.sha1(b'blob ' + str(len(canonical)).encode() + b'\0' + canonical).hexdigest()
            except UnicodeDecodeError:
                pass
        if blob != entry['sha']:
            raise ValueError('本地源码与初始发布版不一致：' + entry['path'])
        files[entry['path']] = hashlib.sha256(content).hexdigest()
    return validate_manifest({'format': 1, 'repository': REPOSITORY, 'version': installed_version,
                              'commit': commit, 'data_format': DATA_FORMAT, 'files': files})


def plan_update(root, before, after):
    validate_manifest(before)
    validate_manifest(after)
    if version(after['version']) <= version(before['version']):
        raise ValueError('目标版本不高于已安装版本；升级工具不执行降级。')
    previous_names = {name.casefold(): name for name in before['files']}
    if any(name.casefold() in previous_names and previous_names[name.casefold()] != name for name in after['files']):
        raise ValueError('升级包包含仅大小写不同的路径变更，请维护者调整文件名后重新发布。')
    for name, checksum in before['files'].items():
        path = safe_path(root, name)
        if not path.is_file() or digest(path) != checksum:
            raise ValueError('本地源码已修改，请先保存并处理差异：' + name)
    changed = []
    for name, checksum in after['files'].items():
        path = safe_path(root, name)
        if name not in before['files'] and path.exists() and (not path.is_file() or digest(path) != checksum):
            raise ValueError('新版本文件与本地自定义文件冲突：' + name)
        if not path.is_file() or digest(path) != checksum:
            changed.append(name)
    return changed, sorted(before['files'].keys() - after['files'].keys())


def clean_environment():
    # Stage processes never inherit model credentials or production DB URLs.
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
               'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HTTP_PROXY', 'HTTPS_PROXY',
               'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
               'PG_BIN'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    return dict(env, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1')


def prepare_candidate(candidate, backup_directory):
    # Do not import the install wizard: bootstrapping an older release must not
    # import that release's runtime_core as if it already contained new modules.
    node, npm = shutil.which('node'), shutil.which('npm')
    if sys.version_info < (3, 12) or not node or not npm:
        raise ValueError('升级需要 Python 3.12+ 和 Node.js 22.12+（包含 npm）。')
    node_version = subprocess.check_output([node, '--version'], text=True).strip().removeprefix('v')
    if version(node_version) < (22, 12, 0):
        raise ValueError('请先安装 Node.js 22.12+ 或 24 LTS。')
    print('正在安装独立的新版本依赖环境……', flush=True)
    environment = candidate / '.venv'
    subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True, env=clean_environment())
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(candidate / 'backend/requirements.txt')],
                   check=True, cwd=candidate, env=clean_environment())
    print('正在安装前端依赖并构建网页……', flush=True)
    for arguments in (['ci'], ['run', 'build']):
        subprocess.run([npm, *arguments], cwd=candidate / 'frontend-v2', check=True, env=clean_environment())
    print('正在使用数据库副本验证启动（不调用 AI）……', flush=True)
    subprocess.run([str(python), '-X', 'utf8', str(candidate / 'tools/upgrade_probe.py'), str(backup_directory)],
                   cwd=candidate, check=True, env=clean_environment())
    return python


def recover(root):
    """Caller holds both installation and runtime locks. No database writes."""
    marker = internal_path(root, '.updates/pending.json')
    if not marker.exists():
        return False
    pending = read_json(marker)
    transaction = internal_path(root, pending['transaction'])
    if transaction.parent != root / '.updates/transactions':
        raise ValueError('Invalid recovery transaction')
    journal = read_json(transaction / 'journal.json')
    if journal['status'] == 'complete':
        marker.unlink()
        return True
    # Validate the complete source backup before any restoration.
    for name, entry in journal['files'].items():
        path = internal_path(root, name) if name in {MANIFEST, '.updates/current.json'} else safe_path(root, name)
        if entry['before'] is not None and digest(transaction / 'source' / name) != entry['before']:
            raise ValueError('Recovery backup checksum mismatch')
        current = digest(path) if path.is_file() else None
        if current not in {entry['before'], entry['after']}:
            raise ValueError('恢复前检测到新的本地修改：' + name)
    def restore_file(name, entry):
        path = internal_path(root, name) if name in {MANIFEST, '.updates/current.json'} else safe_path(root, name)
        if entry['before'] is None:
            if path.exists():
                path.unlink()
        else:
            atomic_bytes(path, (transaction / 'source' / name).read_bytes())
    guards = ['backend/serve.py', 'tools/start.py', 'runtime_core/installation.py']
    for name, entry in journal['files'].items():
        if name not in guards:
            restore_file(name, entry)
    dist = internal_path(root, 'frontend-v2/dist')
    old_dist = transaction / 'previous-dist'
    if old_dist.exists():
        if dist.exists():
            os.replace(dist, transaction / ('failed-dist-' + uuid.uuid4().hex))
        os.replace(old_dist, dist)
    elif not journal['had_dist'] and dist.exists():
        os.replace(dist, transaction / ('failed-dist-' + uuid.uuid4().hex))
    # Keep the interrupted-upgrade guard until the old program and frontend
    # are fully restored, including when recovering the original first release.
    for name in guards:
        if name in journal['files']:
            restore_file(name, journal['files'][name])
    journal['status'] = 'recovered'
    write_json(transaction / 'journal.json', journal)
    marker.unlink()
    return True


def apply_candidate(root, transaction, before, after, python, *, replace_file=atomic_bytes):
    """Candidate is built/verified and databases are backed up before this call."""
    candidate = transaction / 'candidate'
    changed, removed = plan_update(root, before, after)
    for name, checksum in after['files'].items():
        if digest(safe_path(candidate, name)) != checksum:
            raise ValueError('Candidate source changed during preparation')
    # Install the interrupted-upgrade guard before other changed source files.
    first = ['runtime_core/installation.py', 'tools/start.py', 'backend/serve.py']
    changed.sort(key=lambda name: (first.index(name) if name in first else len(first), name))
    new_state = {'format': 1, 'manifest': after, 'python': str(python.relative_to(root).as_posix())}
    special = {MANIFEST: (candidate / MANIFEST).read_bytes(),
               '.updates/current.json': json.dumps(new_state, ensure_ascii=False, indent=2).encode('utf8')}
    journal = {'format': 1, 'status': 'applying', 'had_dist': (root / 'frontend-v2/dist').exists(), 'files': {}}
    for name in changed + removed + list(special):
        path = internal_path(root, name) if name in special else safe_path(root, name)
        previous = path.read_bytes() if path.exists() else None
        if previous is not None:
            saved = transaction / 'source' / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(previous)
        journal['files'][name] = {
            'before': hashlib.sha256(previous).hexdigest() if previous is not None else None,
            'after': hashlib.sha256(special[name]).hexdigest() if name in special else after['files'].get(name),
        }
    write_json(transaction / 'journal.json', journal)
    write_json(root / '.updates/pending.json', {'transaction': transaction.relative_to(root).as_posix()})
    try:
        for name in changed:
            replace_file(safe_path(root, name), (candidate / name).read_bytes())
        for name in removed:
            safe_path(root, name).unlink()
        dist = internal_path(root, 'frontend-v2/dist')
        if dist.exists():
            os.replace(dist, transaction / 'previous-dist')
        os.replace(candidate / 'frontend-v2/dist', dist)
        for name, content in special.items():
            replace_file(internal_path(root, name), content)
        journal['status'] = 'complete'
        write_json(transaction / 'journal.json', journal)
        (root / '.updates/pending.json').unlink()
    except BaseException:
        recover(root)
        raise
    return {'changed': len(changed), 'removed': len(removed), 'backup': str(transaction / 'databases')}


def upgrade(root, archive, backup_module, config, env_file, *, prepare=prepare_candidate, expected_version=None):
    """Caller holds both maintenance locks. Keep the old environment intact."""
    transaction = internal_path(root, '.updates/transactions/' + uuid.uuid4().hex)
    transaction.mkdir(parents=True)
    candidate = transaction / 'candidate'
    after = unpack_package(archive, candidate)
    if expected_version and after['version'] != expected_version:
        raise ValueError('Package version differs from GitHub release')
    before = baseline(root)
    plan_update(root, before, after)
    print('正在备份平台和各酒馆数据库……', flush=True)
    backup_module.backup_locked(transaction / 'databases', config)
    if Path(env_file).is_file():
        shutil.copyfile(env_file, transaction / 'private.env')
    python = prepare(candidate, transaction / 'databases')
    if not python.is_file() or not (candidate / 'frontend-v2/dist/index.html').is_file():
        raise ValueError('Candidate environment or frontend is incomplete')
    result = apply_candidate(root, transaction, before, after, python)
    return after['version'], result
