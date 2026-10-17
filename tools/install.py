"""Install into a private virtual environment and build the self-hosted frontend."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 or newer is required')
    npm = shutil.which('npm')
    if not npm:
        raise SystemExit('Install Node.js 22.12+ or 24 LTS (including npm) first')
    directory = ROOT / '.venv'
    if not directory.exists():
        venv.EnvBuilder(with_pip=True).create(directory)
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', 'pip==26.2.1'], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'backend/requirements.txt')], check=True)
    subprocess.run([str(python), str(ROOT / 'tools/configure.py'), '--env-file',
                    os.getenv('PLATFORM_ENV_FILE', str(ROOT / '.env'))], check=True)
    for args in (['ci'], ['run', 'build']):
        subprocess.run([npm, *args], cwd=ROOT / 'frontend-v2', check=True)
    print('Installed. Configure your model provider privately, create an administrator, then run backend/serve.py with .venv Python. See docs/DEPLOYMENT.md.')


if __name__ == '__main__':
    main()
