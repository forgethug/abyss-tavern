"""Interactive installer. Run with Python 3.12+; no model calls are made."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class SetupError(RuntimeError):
    """A safe, actionable message containing no user-supplied secrets."""


def check_environment():
    if sys.version_info < (3, 12):
        raise SetupError('请先安装 Python 3.12 或更高版本，再运行安装向导。')
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        raise SetupError('请先安装 Node.js 22.12+ 或 24 LTS（包含 npm），再重新打开终端。')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (22, 12, 0):
        raise SetupError('Node.js 版本过低，请安装 22.12+ 或 24 LTS。')


def ensure_admin():
    # Import only after the private env file is selected and dependencies exist.
    sys.path.insert(0, str(ROOT / 'backend'))
    import click
    from app import create_app
    from config import Config
    from extensions import db
    from models import User
    from runtime_core.process_lock import instance_lock
    from werkzeug.security import generate_password_hash

    with instance_lock(Config.RUNTIME_DATA_DIR):
        app = create_app()
        with app.app_context():
            if db.session.query(User).filter_by(is_admin=True, is_active=True).first():
                print('已有管理员账号，保留原账号和密码。')
                return
            print('创建首个管理员。用户名使用英文字母、数字或下划线，长度 3–32。')
            username = click.prompt('管理员用户名')
            if (not Config.USERNAME_MIN_LENGTH <= len(username) <= Config.USERNAME_MAX_LENGTH
                    or not re.fullmatch(Config.USERNAME_PATTERN, username)):
                raise SetupError('用户名格式不正确，请重新运行安装向导。')
            if db.session.query(User).filter_by(username=username).first():
                raise SetupError('该用户名已被使用；向导不会覆盖已有账号，请使用新的管理员用户名。')
            password = click.prompt('管理员密码', hide_input=True, confirmation_prompt=True)
            if not Config.PASSWORD_MIN_LENGTH <= len(password) <= Config.PASSWORD_MAX_LENGTH:
                raise SetupError('密码长度必须为 6–64，请重新运行安装向导。')
            db.session.add(User(username=username, display_name=username,
                password_hash=generate_password_hash(password), is_admin=True, is_active=True))
            db.session.commit()
            print('管理员已创建。请记住账号和密码。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='深渊酒馆安装与启动向导')
    parser.add_argument('--configure-only', action='store_true', help='已安装依赖时，只配置模型和管理员')
    parser.add_argument('--no-start', action='store_true', help='完成配置后不启动服务')
    parser.add_argument('--env-file', type=Path, default=Path(os.getenv('PLATFORM_ENV_FILE', str(ROOT / '.env'))))
    args = parser.parse_args(argv)
    env_path = args.env_file.resolve()
    os.environ['PLATFORM_ENV_FILE'] = str(env_path)
    os.environ['PYTHONUTF8'] = '1'
    if not args.configure_only:
        check_environment()
        print('正在安装依赖并构建网页，首次运行需要联网下载，请耐心等待。', flush=True)
        subprocess.run([sys.executable, str(ROOT / 'tools/install.py')], cwd=ROOT, check=True)
        python = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        command = [str(python), '-X', 'utf8', str(Path(__file__).resolve()), '--configure-only', '--env-file', str(env_path)]
        if args.no_start:
            command.append('--no-start')
        return subprocess.call(command, cwd=ROOT)

    from configure import configure, configure_model
    configure(env_path)
    configure_model(env_path)
    ensure_admin()
    if args.no_start:
        print('配置完成。以后运行 tools/start.py 启动，不需要重新安装。')
        return 0
    from start import main as start
    return start(['--env-file', str(env_path)])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。已保存的配置会保留，下次可以继续。')
        raise SystemExit(1)
    except subprocess.CalledProcessError:
        raise SystemExit('安装未完成，请查看上方出错的步骤并重试。') from None
    except SetupError as exc:
        raise SystemExit(str(exc)) from None
    except (RuntimeError, ValueError):
        # Do not expose provider credentials through configuration/DB exceptions.
        raise SystemExit('设置未完成。请核对 Python/Node 版本、输入格式，并确保已有服务已停止。详见 docs/DEPLOYMENT.md。') from None
