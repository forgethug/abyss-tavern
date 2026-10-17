"""Versioned, data-free release packages shared by the publisher and updater."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from export_public import allowed

REPOSITORY = 'w7lau/abyss-tavern'
MANIFEST = 'release-manifest.json'
DATA_FORMAT = 1
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('Invalid stable version')
    return tuple(map(int, value.split('.')))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def valid_path(name):
    if not isinstance(name, str) or not name or len(name) > 240:
        return False
    path = PurePosixPath(name)
    if path.as_posix() != name or path.is_absolute() or not allowed(name):
        return False
    for part in path.parts:
        if part in {'.', '..'} or part.endswith((' ', '.')) or any(ord(c) < 32 or c in '\\:*?"<>|' for c in part):
            return False
        if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part):
            return False
    return True


def safe_path(root, name):
    if not valid_path(name):
        raise ValueError('Unsafe release path')
    root = Path(root).resolve()
    path = root / name
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('Release path crosses a link')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Release path escapes installation')
    return path


def validate_manifest(value):
    if not isinstance(value, dict) or value.get('format') != 1 or value.get('repository') != REPOSITORY:
        raise ValueError('Unknown release manifest')
    version(value.get('version'))
    if value.get('data_format') != DATA_FORMAT:
        raise ValueError('This release requires a different database migration protocol')
    if not re.fullmatch(r'[0-9a-f]{40}', value.get('commit', '')):
        raise ValueError('Release must identify an immutable source commit')
    files = value.get('files')
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise ValueError('Invalid release file list')
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError('Case-colliding release paths')
    for name, checksum in files.items():
        if name == MANIFEST or not valid_path(name) or not re.fullmatch(r'[0-9a-f]{64}', str(checksum)):
            raise ValueError('Invalid release file or checksum')
    return value


def unpack_package(archive, destination):
    destination = Path(destination)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(names) > 10001:
            raise ValueError('Duplicate or excessive package entries')
        if sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('Expanded package is too large')
        for entry in entries:
            if entry.is_dir() or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG):
                raise ValueError('Package contains a link or non-file')
            if entry.filename != MANIFEST and not valid_path(entry.filename):
                raise ValueError('Unsafe package entry')
        if MANIFEST not in names or package.getinfo(MANIFEST).file_size > 2 * 1024 * 1024:
            raise ValueError('Missing or oversized manifest')
        manifest = validate_manifest(json.loads(package.read(MANIFEST)))
        if set(names) != set(manifest['files']) | {MANIFEST}:
            raise ValueError('Package contents do not match manifest')
        destination.mkdir(parents=True, exist_ok=False)
        for name, checksum in manifest['files'].items():
            content = package.read(name)
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError('Release file checksum mismatch')
            path = safe_path(destination, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (destination / MANIFEST).write_bytes(package.read(MANIFEST))
    return manifest
