"""Export an allowlisted source snapshot without private Git history or runtime data."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.env.example', '.gitattributes', '.gitignore', 'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'}
TREES = {'backend', 'runtime_core', 'runtime_host', 'game-packs', 'frontend-v2', 'tools', 'deploy', '.github'}
PUBLIC_DOCS = {'DEPLOYMENT.md', 'DEPLOYMENT.en.md', 'ADMIN.md', 'ADMIN.en.md'}
PUBLIC_MARKDOWN = {
    'README.md', 'README.en.md', 'THIRD_PARTY_NOTICES.md',
    'docs/DEPLOYMENT.md', 'docs/DEPLOYMENT.en.md', 'runtime_host/README.md',
    'docs/ADMIN.md', 'docs/ADMIN.en.md',
    'game-packs/s1-fantasy/README.md', 'game-packs/s1-fantasy/SELF_HOSTED_RULES.md',
    'game-packs/s1-fantasy/PLAYING.en.md',
}
FORBIDDEN_PARTS = {'.git', '.codex', '.agents', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
                   'data', 'dist', 'logs', '.tmp', 'backups'}
SECRET_PATTERNS = [re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
                   re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
                   re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b')]


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    if path.parts[0] == 'game-packs' and path.parts[1:2] != ('s1-fantasy',):
        return False
    if name.startswith('frontend-v2/src/plugins/') and not name.startswith('frontend-v2/src/plugins/adventurer_s1/'):
        return False
    if name in {'tools/sync-s0-source-snapshot.ps1', 'runtime_host/tests/test_original_anomaly_runtime.py',
                'backend/tests/test_original_pack_boundaries.py'}:
        return False
    if set(path.parts) & FORBIDDEN_PARTS or path.name == 'AGENTS.md':
        return False
    if path.name.startswith('.env') and name != '.env.example':
        return False
    if path.suffix.lower() == '.md' and name not in PUBLIC_MARKDOWN:
        return False
    if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.log', '.pyc', '.pem', '.key', '.dump', '.zip'}:
        return False
    return name in ROOT_FILES or path.parts[0] in TREES or (path.parts[0] == 'docs' and path.name in PUBLIC_DOCS)


def export(destination, ref=None, working_tree=False):
    if bool(ref) == bool(working_tree):
        raise ValueError('Choose exactly one of --ref or --working-tree (local validation only)')
    files = {}
    if working_tree:
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
        for name in names:
            if name and allowed(name):
                path = ROOT / name
                if path.is_symlink():
                    raise ValueError('Symlinks are not exported')
                if path.is_file():
                    files[name] = path.read_bytes()
        revision = 'uncommitted-local-validation'
    else:
        revision = subprocess.check_output(['git', 'rev-parse', '--verify', ref + '^{commit}'], cwd=ROOT).decode().strip()
        archive = subprocess.check_output(['git', 'archive', '--format=tar', revision], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if allowed(member.name) and not member.isdir():
                    if not member.isfile():
                        raise ValueError('Non-regular archive member')
                    files[member.name] = tar.extractfile(member).read()
    if not {'LICENSE', 'THIRD_PARTY_NOTICES.md', '.env.example', 'backend/serve.py'} <= files.keys():
        raise ValueError('Release is missing mandatory files')
    for name, data in files.items():
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise ValueError(f'Potential secret in {name}; inspect privately before export')
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'format': 1, 'source_revision': revision, 'files': {}}
    for name, data in sorted(files.items()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    (destination / 'RELEASE.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--ref', help='Committed release tag or revision')
    group.add_argument('--working-tree', action='store_true', help='Local validation snapshot; not a tagged release')
    args = parser.parse_args()
    print(f'Exported {export(args.destination, args.ref, args.working_tree)} files; no repository history copied.')
