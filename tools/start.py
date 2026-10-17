"""Start the installed tavern without rebuilding or changing configuration."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_core.installation import installed_python, require_completed_update


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动深渊酒馆')
    parser.add_argument('--env-file', type=Path, default=Path(os.getenv('PLATFORM_ENV_FILE', str(ROOT / '.env'))))
    args = parser.parse_args(argv)
    require_completed_update(ROOT)
    python = installed_python(ROOT)
    if not python.is_file() or not args.env_file.is_file() or not (ROOT / 'frontend-v2/dist/index.html').is_file():
        raise SystemExit('尚未完成安装，请先运行 python tools/setup.py。')
    env = dict(os.environ, PLATFORM_ENV_FILE=str(args.env_file.resolve()), PYTHONUTF8='1')
    print('正在启动深渊酒馆。默认访问 http://127.0.0.1:5500；自定义地址以启动输出为准。', flush=True)
    print('请保持本窗口运行；停止服务时按 Ctrl+C。', flush=True)
    child = subprocess.Popen([str(python), '-X', 'utf8', str(ROOT / 'backend/serve.py')], cwd=ROOT, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        # Windows console broadcasts Ctrl+C to both processes. On POSIX the
        # foreground process group receives it as well; allow normal cleanup.
        return child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
