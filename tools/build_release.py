"""Build a checksummed upgrade package from a clean public release commit."""
import argparse
import json
from pathlib import Path
import tempfile
import zipfile

from export_public import export
from release_format import DATA_FORMAT, MANIFEST, REPOSITORY, digest, validate_manifest, version


def build(destination, ref, release_version):
    version(release_version)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'source'
        export(source, ref=ref)
        exported = json.loads((source / 'RELEASE.json').read_text(encoding='utf8'))
        if (source / 'VERSION').read_text(encoding='utf8').strip() != release_version:
            raise ValueError('Tag version differs from VERSION')
        manifest = validate_manifest({
            'format': 1, 'repository': REPOSITORY, 'version': release_version,
            'commit': exported['source_revision'], 'data_format': DATA_FORMAT,
            'files': exported['files'],
        })
        name = f'abyss-tavern-v{release_version}.zip'
        archive = destination / name
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
            for relative in sorted(manifest['files']):
                package.write(source / relative, relative)
            package.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        (destination / (name + '.sha256')).write_text(f'{digest(archive)}  {name}\n', encoding='ascii')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output, args.ref, args.version))
